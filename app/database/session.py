"""Async SQLAlchemy engine and session-factory construction."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings

MYSQL_UTC_INIT_COMMAND = "SET time_zone='+00:00'"


def create_async_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Create an async MySQL engine anchored to UTC without exposing credentials."""

    url = settings.database_url.get_secret_value()
    connect_args: dict[str, object] = {}
    try:
        if make_url(url).get_backend_name() == "mysql":
            connect_args["init_command"] = MYSQL_UTC_INIT_COMMAND
    except Exception:
        pass
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return independent async sessions that retain committed ORM values."""

    return async_sessionmaker(engine, expire_on_commit=False)
