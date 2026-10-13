"""FastAPI + aiogram 3 + APScheduler: Telegram Mini App «Планер дня».

Запуск:  python main.py   (или: uvicorn main:app --host 0.0.0.0 --port 8000)
Бот работает в режиме long polling внутри lifespan FastAPI.
"""
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    SessionLocal,
    Task,
    User,
    get_all_telegram_ids,
    get_day_stats,
    get_or_create_user,
    get_tasks_for_day,
    init_db,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("planner")

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Novokuznetsk"))
INIT_DATA_MAX_AGE = 24 * 60 * 60  # initData старше суток не принимаем

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)


def today() -> date:
    return datetime.now(TZ).date()


def open_app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть планер", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


# --------------------------------------------------------------------------- #
#  Авторизация Mini App: проверка подписи initData
#  https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
# --------------------------------------------------------------------------- #
def validate_init_data(init_data: str) -> dict:
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            raise ValueError("bad hash")
        if time.time() - int(parsed["auth_date"]) > INIT_DATA_MAX_AGE:
            raise ValueError("expired")
        return json.loads(parsed["user"])
    except (KeyError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid initData")


async def get_session():
    async with SessionLocal() as session:
        yield session


async def current_user(
    x_init_data: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> User:
    tg_user = validate_init_data(x_init_data)
    return await get_or_create_user(session, int(tg_user["id"]))


# --------------------------------------------------------------------------- #
#  Схемы API
# --------------------------------------------------------------------------- #
class TaskCreate(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class TaskUpdate(BaseModel):
    is_completed: bool


def task_out(t: Task) -> dict:
    return {"id": t.id, "text": t.text, "is_completed": t.is_completed}


# --------------------------------------------------------------------------- #
#  Бот
# --------------------------------------------------------------------------- #
@dp.message(CommandStart())
async def cmd_start(message: Message):
    async with SessionLocal() as session:
        await get_or_create_user(session, message.from_user.id)
    await message.answer(
        "Привет! Это планер на каждый день.\n"
        "Запишите задачи на сегодня и отмечайте выполненные. "
        "Утром я напомню составить план, а вечером пришлю итоги.",
        reply_markup=open_app_keyboard(),
    )


# --------------------------------------------------------------------------- #
#  Планировщик
# --------------------------------------------------------------------------- #
async def safe_send(chat_id: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — удаляем его вместе с задачами
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.telegram_id == chat_id))
            await session.commit()
    except TelegramAPIError as e:
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
    await asyncio.sleep(0.05)  # мягкий лимит ~20 сообщений/сек


async def morning_reminder() -> None:
    async with SessionLocal() as session:
        ids = await get_all_telegram_ids(session)
    for tg_id in ids:
        await safe_send(
            tg_id,
            "Доброе утро! Самое время записать задачи на сегодня.",
            reply_markup=open_app_keyboard(),
        )


async def evening_report() -> None:
    day = today()
    async with SessionLocal() as session:
        users = (await session.execute(User.__table__.select())).all()
        for u in users:
            total, done = await get_day_stats(session, u.id, day)
            if total == 0:
                text = "Сегодня в планере пусто. Завтра с утра можно начать с одной задачи."
            else:
                percent = round(done / total * 100)
                text = f"<b>Итоги дня</b>\nВыполнено {done} из {total} ({percent}%)."
                if done == total:
                    text += "\nВсе задачи закрыты. Хороший день."
                else:
                    left = [t for t in await get_tasks_for_day(session, u.id, day) if not t.is_completed]
                    shown = "\n".join(f"• {html.escape(t.text)}" for t in left[:10])
                    more = f"\nи ещё {len(left) - 10}" if len(left) > 10 else ""
                    text += f"\n\nНе закрыто:\n{shown}{more}"
            await safe_send(u.telegram_id, text, reply_markup=open_app_keyboard())


# --------------------------------------------------------------------------- #
#  FastAPI
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    scheduler.add_job(morning_reminder, CronTrigger(hour=8, minute=0, timezone=TZ))
    scheduler.add_job(evening_report, CronTrigger(hour=21, minute=0, timezone=TZ))
    scheduler.start()

    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Планер", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    log.info("Бот и планировщик запущены")
    try:
        yield
    finally:
        polling.cancel()
        scheduler.shutdown(wait=False)
        await bot.session.close()


app = FastAPI(title="Daily Planner", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/tasks")
async def list_tasks(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    day = today()
    tasks = await get_tasks_for_day(session, user.id, day)
    return {"date": day.isoformat(), "tasks": [task_out(t) for t in tasks]}


@app.post("/api/tasks", status_code=201)
async def create_task(
    body: TaskCreate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Пустая задача")
    task = Task(user_id=user.id, text=text, date=today())
    session.add(task)
    await session.commit()
    return task_out(task)


async def _own_task(session: AsyncSession, user: User, task_id: int) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(404, "Задача не найдена")
    return task


@app.patch("/api/tasks/{task_id}")
async def update_task(
    task_id: int,
    body: TaskUpdate,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    task = await _own_task(session, user, task_id)
    task.is_completed = body.is_completed
    await session.commit()
    return task_out(task)


@app.delete("/api/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    task = await _own_task(session, user, task_id)
    await session.delete(task)
    await session.commit()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000)

from fastapi import FastAPI

app = FastAPI()

# Твой основной код бота, роутеры для Mini App и т.д.

# Добавляем этот простейший маршрут:
@app.get("/ping")
async def ping():
    return {"status": "alive", "message": "Bot is awake!"}
