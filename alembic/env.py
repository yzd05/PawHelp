"""Alembic environment for the application's async MySQL connection."""

import asyncio
import importlib.util
from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.database.base import Base
import app.database.models  # noqa: F401
from app.database.session import MYSQL_UTC_INIT_COMMAND


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _normalize_driver(url: str) -> str:
    if (
        url.startswith("mysql+aiomysql://")
        and importlib.util.find_spec("aiomysql") is None
        and importlib.util.find_spec("asyncmy") is not None
    ):
        return "mysql+asyncmy://" + url.removeprefix("mysql+aiomysql://")
    return url


def _resolve_database_url(*, offline: bool) -> str:
    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        return _normalize_driver(env_url)
    configured = (config.get_main_option("sqlalchemy.url") or "").strip()
    if configured and not configured.startswith("driver://"):
        return _normalize_driver(configured)
    try:
        from app.config import get_settings

        return _normalize_driver(get_settings().database_url.get_secret_value())
    except Exception:
        if offline:
            return "mysql+asyncmy://offline:offline@127.0.0.1:3306/pawhelp"
        raise


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = _resolve_database_url(offline=True)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = _resolve_database_url(offline=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    connect_args: dict[str, object] = {}
    try:
        if make_url(url).get_backend_name() == "mysql":
            connect_args["init_command"] = MYSQL_UTC_INIT_COMMAND
    except Exception:
        pass
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
