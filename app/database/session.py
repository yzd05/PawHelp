"""Async SQLAlchemy engine and session-factory construction."""

from contextlib import suppress

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

MYSQL_UTC_INIT_COMMAND = "SET time_zone='+00:00'"


def create_async_engine_from_settings(
    settings: Settings,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    pool_recycle: int | None = None,
) -> AsyncEngine:
    """Create an async MySQL engine anchored to UTC without exposing credentials."""

    url = settings.database_url.get_secret_value()
    connect_args: dict[str, object] = {}
    backend = ""
    with suppress(ArgumentError, ValueError):
        backend = make_url(url).get_backend_name()
        if backend == "mysql":
            connect_args["init_command"] = MYSQL_UTC_INIT_COMMAND
    is_sqlite = backend == "sqlite" or url.lstrip().lower().startswith("sqlite")
    engine_kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "connect_args": connect_args,
    }
    if not is_sqlite:
        engine_kwargs.update(
            pool_size=getattr(settings, "db_pool_size", 10) if pool_size is None else pool_size,
            max_overflow=getattr(settings, "db_max_overflow", 20) if max_overflow is None else max_overflow,
            pool_recycle=(
                getattr(settings, "db_pool_recycle_seconds", 1800) if pool_recycle is None else pool_recycle
            ),
        )
    return create_async_engine(url, **engine_kwargs)


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return independent async sessions that retain committed ORM values."""

    return async_sessionmaker(engine, expire_on_commit=False)
