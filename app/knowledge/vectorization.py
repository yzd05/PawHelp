"""Recoverable MySQL-to-Milvus knowledge vectorization flow."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import logging
from typing import Protocol

from pymilvus import exceptions as milvus_exceptions

from app.database.models import KnowledgeChunk
from app.knowledge.vector_store import milvus_call, supports_recreate_flag

logger = logging.getLogger(__name__)

_TRANSIENT_MILVUS_EXCEPTIONS = tuple(
    cls
    for name in (
        "ConnectError",
        "ConnectionNotExistException",
        "ConnectionConfigException",
        "MilvusUnavailableException",
    )
    if (cls := getattr(milvus_exceptions, name, None)) is not None
)
_BASE_MILVUS_EXCEPTION = getattr(milvus_exceptions, "MilvusException", ())


def _is_transient_outage(error: BaseException) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True
    if _TRANSIENT_MILVUS_EXCEPTIONS and isinstance(error, _TRANSIENT_MILVUS_EXCEPTIONS):
        return True
    return False


def _is_poison_pill_error(error: BaseException) -> bool:
    if _is_transient_outage(error):
        return False
    if isinstance(error, (ValueError, TypeError)):
        return True
    if _BASE_MILVUS_EXCEPTION and isinstance(error, _BASE_MILVUS_EXCEPTION):
        return True
    return False


class PendingKnowledge(Protocol):
    async def list_pending(self, *, limit: int) -> list[KnowledgeChunk]: ...

    async def mark_vectorized(self, rows: Sequence[KnowledgeChunk]) -> None: ...


class ReindexKnowledge(Protocol):
    async def list_after_id(
        self, *, after_id: int, limit: int
    ) -> list[KnowledgeChunk]: ...


class ReconcileKnowledge(Protocol):
    async def list_vectorized_after_id(
        self, *, after_id: int, limit: int
    ) -> list[KnowledgeChunk]: ...

    async def mark_pending(self, rows: Sequence[KnowledgeChunk]) -> None: ...


class DenseEmbedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class KnowledgeVectorStore(Protocol):
    def ensure_collection(self) -> None: ...

    def upsert(
        self,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        texts: Sequence[str],
        metadata: Sequence[dict[str, str]],
    ) -> None: ...

    def existing_ids(self, ids: Sequence[str]) -> set[str]: ...


def vector_text(row: KnowledgeChunk) -> str:
    """Keep metadata out of the representation by contract."""

    return f"分类：{row.category}\n问题：{row.questions}\n答案：{row.answer}"


def _metadata(rows: Sequence[KnowledgeChunk]) -> list[dict[str, str]]:
    return [
        {
            "category": row.category,
            "content_type": row.content_type or "",
            "section_path": row.section_path or "",
        }
        for row in rows
    ]


async def _mark_failed_rows(repository: object, rows: Sequence[KnowledgeChunk]) -> bool:
    mark_failed = getattr(repository, "mark_failed", None)
    if callable(mark_failed):
        await mark_failed(rows)
        return True
    for row in rows:
        row.vector_id = None
        row.vectorize_status = "failed"
    return True


async def vectorize_pending(
    repository: PendingKnowledge,
    embedder: DenseEmbedder,
    vector_store: KnowledgeVectorStore,
    commit: Callable[[], Awaitable[None]],
    *,
    batch_size: int,
) -> int:
    """Upsert pending rows in committed batches, isolating poison-pill rows as ``failed``."""

    await milvus_call(vector_store.ensure_collection)
    completed = 0
    while rows := await repository.list_pending(limit=batch_size):
        texts = [vector_text(row) for row in rows]
        try:
            vectors = await asyncio.to_thread(embedder.embed, texts)
            embed_ready: list[tuple[KnowledgeChunk, str, list[float], str, dict[str, str]]] = [
                (row, str(row.id), vec, text, meta)
                for row, vec, text, meta in zip(rows, vectors, texts, _metadata(rows), strict=True)
            ]
            embed_failed: list[tuple[KnowledgeChunk, Exception]] = []
        except Exception as embed_error:
            if len(rows) == 1 and not _is_poison_pill_error(embed_error):
                raise
            embed_ready = []
            embed_failed = []
            for row, text, meta in zip(rows, texts, _metadata(rows), strict=True):
                try:
                    single_vec = (await asyncio.to_thread(embedder.embed, [text]))[0]
                    embed_ready.append((row, str(row.id), single_vec, text, meta))
                except Exception as item_error:
                    embed_failed.append((row, item_error))
            if not embed_ready and not all(_is_poison_pill_error(err) for _, err in embed_failed):
                raise embed_error

        if embed_failed:
            bad_rows = [row for row, _ in embed_failed]
            for row, err in embed_failed:
                logger.warning("isolating chunk id=%s after embedding error: %s", row.id, err)
            await _mark_failed_rows(repository, bad_rows)
            if not embed_ready:
                await commit()
                continue

        ready_rows = [item[0] for item in embed_ready]
        ids = [item[1] for item in embed_ready]
        ready_vectors = [item[2] for item in embed_ready]
        ready_texts = [item[3] for item in embed_ready]
        ready_meta = [item[4] for item in embed_ready]

        try:
            await milvus_call(vector_store.upsert, ids, ready_vectors, ready_texts, ready_meta)
            await repository.mark_vectorized(ready_rows)
            await commit()
            completed += len(ready_rows)
        except Exception as batch_error:
            if len(ready_rows) == 1:
                if _is_poison_pill_error(batch_error):
                    logger.warning(
                        "isolating poison-pill knowledge chunk id=%s: %s",
                        ready_rows[0].id,
                        batch_error,
                    )
                    await _mark_failed_rows(repository, ready_rows)
                    await commit()
                    continue
                if embed_failed:
                    await commit()
                raise

            succeeded_rows: list[KnowledgeChunk] = []
            failed_upserts: list[tuple[KnowledgeChunk, Exception]] = []
            for row, vid, vec, text, meta in embed_ready:
                try:
                    await milvus_call(vector_store.upsert, [vid], [vec], [text], [meta])
                    succeeded_rows.append(row)
                except Exception as row_error:
                    failed_upserts.append((row, row_error))

            # If other rows in the same batch succeeded, Milvus is reachable and
            # non-transient failures on individual rows are isolated as poison pills.
            isolatable = [
                (row, err)
                for row, err in failed_upserts
                if _is_poison_pill_error(err) or (succeeded_rows and not _is_transient_outage(err))
            ]
            transient = [(row, err) for row, err in failed_upserts if (row, err) not in isolatable]

            if not succeeded_rows and not isolatable:
                if embed_failed:
                    await commit()
                raise batch_error

            if succeeded_rows:
                await repository.mark_vectorized(succeeded_rows)
                completed += len(succeeded_rows)
            if isolatable:
                for row, err in isolatable:
                    logger.warning(
                        "isolating poison-pill knowledge chunk id=%s during fallback: %s",
                        row.id,
                        err,
                    )
                await _mark_failed_rows(repository, [row for row, _ in isolatable])
            await commit()
            if transient:
                raise transient[0][1]
    return completed


async def reconcile_missing_vectors(
    repository: ReconcileKnowledge,
    vector_store: KnowledgeVectorStore,
    commit: Callable[[], Awaitable[None]],
    *,
    batch_size: int,
) -> int:
    """Reconcile MySQL and Milvus in both directions: tombstones, missing vectors, and orphans."""

    repaired = 0
    list_deleting = getattr(repository, "list_deleting", None)
    delete_ids = getattr(repository, "delete_ids", None)
    store_delete = getattr(vector_store, "delete", None)
    if callable(list_deleting) and callable(delete_ids):
        while deleting_rows := await list_deleting(limit=batch_size):
            chunk_ids = [row.id for row in deleting_rows]
            if callable(store_delete):
                await milvus_call(store_delete, [str(chunk_id) for chunk_id in chunk_ids])
            await delete_ids(chunk_ids)
            await commit()
            repaired += len(chunk_ids)

    valid_done_ids: set[str] = set()
    after_id = 0
    while rows := await repository.list_vectorized_after_id(
        after_id=after_id,
        limit=batch_size,
    ):
        ids = [str(row.id) for row in rows]
        existing = await milvus_call(vector_store.existing_ids, ids)
        valid_done_ids.update(existing & set(ids))
        missing = [row for row in rows if str(row.id) not in existing]
        if missing:
            await repository.mark_pending(missing)
            await commit()
            repaired += len(missing)
        after_id = rows[-1].id

    list_all_ids = getattr(vector_store, "list_all_ids", None)
    if callable(list_all_ids) and callable(store_delete):
        milvus_ids = await milvus_call(list_all_ids)
        orphan_ids = sorted(milvus_ids - valid_done_ids)
        for offset in range(0, len(orphan_ids), batch_size):
            batch = orphan_ids[offset : offset + batch_size]
            await milvus_call(store_delete, batch)
            repaired += len(batch)

    return repaired


async def rebuild_collection(
    repository: ReindexKnowledge,
    embedder: DenseEmbedder,
    vector_store: KnowledgeVectorStore,
    *,
    batch_size: int,
) -> int:
    """Idempotently copy every MySQL chunk into a fresh Milvus collection.

    This intentionally ignores ``vectorize_status``: existing rows may already
    point at the legacy dense-only collection. Milvus upsert keeps reruns safe.
    """

    if supports_recreate_flag(vector_store):
        await milvus_call(vector_store.ensure_collection, recreate_incompatible=True)  # type: ignore[call-arg]
    else:
        await milvus_call(vector_store.ensure_collection)
    completed = 0
    after_id = 0
    while rows := await repository.list_after_id(after_id=after_id, limit=batch_size):
        ids = [str(row.id) for row in rows]
        texts = [vector_text(row) for row in rows]
        vectors = await asyncio.to_thread(embedder.embed, texts)
        await milvus_call(vector_store.upsert, ids, vectors, texts, _metadata(rows))
        completed += len(rows)
        after_id = rows[-1].id
    return completed
