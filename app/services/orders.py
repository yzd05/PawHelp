"""Persistent owned order reads with one atomic, non-overwriting demo initialization."""

from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.orders import OrderRepository

_MAX_CACHED_USERS = 4096
_SEEDED_BY_FACTORY: WeakKeyDictionary[object, OrderedDict[str, None]] = WeakKeyDictionary()


def _seeded_cache(
    session_factory: object, fallback: OrderedDict[str, None]
) -> OrderedDict[str, None]:
    try:
        cache = _SEEDED_BY_FACTORY.get(session_factory)
        if cache is None:
            cache = OrderedDict()
            _SEEDED_BY_FACTORY[session_factory] = cache
        return cache
    except TypeError:
        return fallback


def _remember_seeded(cache: OrderedDict[str, None], user_id: str) -> None:
    if not user_id:
        return
    cache[user_id] = None
    cache.move_to_end(user_id)
    while len(cache) > _MAX_CACHED_USERS:
        cache.popitem(last=False)


class OrderData:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._local_seeded: OrderedDict[str, None] = OrderedDict()

    @asynccontextmanager
    async def _repository(self, user_id: str) -> AsyncIterator[OrderRepository]:
        cache = _seeded_cache(self._session_factory, self._local_seeded)
        async with self._session_factory() as session:
            if user_id not in cache:
                async with session.begin():
                    await OrderRepository(session).seed_demo(user_id)
                _remember_seeded(cache, user_id)
            else:
                cache.move_to_end(user_id)
            # A concurrent no-op upsert does not advance a REPEATABLE READ snapshot.
            # Commit initialization before opening a fresh read transaction.
            async with session.begin():
                yield OrderRepository(session)

    async def list_orders(self, user_id: str) -> list[dict[str, object]]:
        async with self._repository(user_id) as repository:
            rows = await repository.list_owned(user_id)
        if not rows and user_id:
            cache = _seeded_cache(self._session_factory, self._local_seeded)
            cache.pop(user_id, None)
            async with self._repository(user_id) as repository:
                return await repository.list_owned(user_id)
        return rows

    async def get_order(self, user_id: str, order_id: str) -> dict[str, object]:
        async with self._repository(user_id) as repository:
            return await repository.get_owned(user_id, order_id)

    async def get_logistics(
        self, user_id: str, *, order_id: str | None = None, tracking_no: str | None = None,
    ) -> dict[str, object]:
        async with self._repository(user_id) as repository:
            return await repository.get_logistics(user_id, order_id=order_id, tracking_no=tracking_no)
