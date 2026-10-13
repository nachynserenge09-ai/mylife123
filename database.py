"""Модели и вспомогательные функции работы с БД (SQLAlchemy 2.0 async + SQLite)."""
import os
from datetime import date

from dotenv import load_dotenv
from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./planner.db")

engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(String(200))
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    date: Mapped[date] = mapped_column(Date, index=True)

    user: Mapped["User"] = relationship(back_populates="tasks")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(session: AsyncSession, telegram_id: int) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
        await session.commit()
    return user


async def get_all_telegram_ids(session: AsyncSession) -> list[int]:
    return list((await session.scalars(select(User.telegram_id))).all())


async def get_tasks_for_day(session: AsyncSession, user_id: int, day: date) -> list[Task]:
    result = await session.scalars(
        select(Task).where(Task.user_id == user_id, Task.date == day).order_by(Task.id)
    )
    return list(result.all())


async def get_day_stats(session: AsyncSession, user_id: int, day: date) -> tuple[int, int]:
    """Возвращает (всего задач, выполнено)."""
    row = (
        await session.execute(
            select(func.count(Task.id), func.coalesce(func.sum(Task.is_completed.cast(BigInteger)), 0))
            .where(Task.user_id == user_id, Task.date == day)
        )
    ).one()
    return int(row[0]), int(row[1])
