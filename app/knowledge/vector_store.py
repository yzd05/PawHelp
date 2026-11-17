"""Milvus collection adapter for stable knowledge vectors."""

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import inspect
import logging
from typing import Callable, ParamSpec, TypeVar

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from app.knowledge.embedding import VECTOR_DIMENSION

logger = logging.getLogger(__name__)

MAX_ID_BYTES = 64
MAX_CATEGORY_BYTES = 1024
MAX_CONTENT_TYPE_BYTES = 32
MAX_SECTION_PATH_BYTES = 2048
MAX_TEXT_BYTES = 65535

_REQUIRED_VARCHAR_LIMITS = {
    "id": MAX_ID_BYTES,
    "category": MAX_CATEGORY_BYTES,
    "content_type": MAX_CONTENT_TYPE_BYTES,
    "section_path": MAX_SECTION_PATH_BYTES,
    "text": MAX_TEXT_BYTES,
}

_P = ParamSpec("_P")
_T = TypeVar("_T")
_MILVUS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="milvus")


def _check_utf8_limit(field: str, value: str, max_bytes: int) -> None:
    size = len(value.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(
            f"Milvus field '{field}' UTF-8 byte length {size} exceeds max_length {max_bytes}"
        )


async def milvus_call(fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Keep the shared synchronous client on one worker across async requests."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_MILVUS_EXECUTOR, partial(fn, *args, **kwargs))


class MilvusKnowledgeStore:
    """Own the minimal Milvus schema and application-provided string IDs."""

    def __init__(self, uri: str, collection_name: str = "knowledge_hybrid") -> None:
        self._uri = uri
        self._client: MilvusClient | None = None
        self._collection_name = collection_name
        self._ensured = False

    @property
    def client(self) -> MilvusClient:
        if self._client is None:
            self._client = MilvusClient(uri=self._uri)
        return self._client

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def close(self) -> None:
        if self._client is not None:
            close_fn = getattr(self._client, "close", None)
            if callable(close_fn):
                close_fn()
            self._client = None
            self._ensured = False

    def _raw_count(self) -> int | None:
        query_fn = getattr(self.client, "query", None)
        if not callable(query_fn):
            return None
        try:
            load_fn = getattr(self.client, "load_collection", None)
            if callable(load_fn):
                load_fn(collection_name=self._collection_name)
            rows = query_fn(
                collection_name=self._collection_name,
                filter="",
                output_fields=["count(*)"],
                consistency_level="Strong",
            )
            return int(rows[0]["count(*)"])
        except Exception:
            return None

    def _incompatible_varchar_fields(self) -> dict[str, tuple[int, int]]:
        describe_fn = getattr(self.client, "describe_collection", None)
        if not callable(describe_fn):
            return {}
        try:
            info = describe_fn(collection_name=self._collection_name)
        except Exception:
            return {}
        fields = info.get("fields") if isinstance(info, dict) else None
        if not isinstance(fields, list):
            return {}
        mismatches: dict[str, tuple[int, int]] = {}
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "")
            expected = _REQUIRED_VARCHAR_LIMITS.get(name)
            if expected is None:
                continue
            params = field.get("params") if isinstance(field.get("params"), dict) else {}
            actual = params.get("max_length", field.get("max_length"))
            if isinstance(actual, int) and actual < expected:
                mismatches[name] = (actual, expected)
        return mismatches

    def count_entities(self) -> int:
        """Count live entities for dual-write diagnostics.

        Collection statistics keep counting deleted rows until compaction, so a
        Markdown re-sync would report a false mismatch; count(*) does not.
        """

        if not self.client.has_collection(collection_name=self._collection_name):
            return 0
        self.ensure_collection()
        rows = self.client.query(
            collection_name=self._collection_name,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
        )
        return int(rows[0]["count(*)"])

    def existing_ids(self, ids: Sequence[str]) -> set[str]:
        """Return the requested primary keys that are actually present."""

        if not ids or not self.client.has_collection(
            collection_name=self._collection_name
        ):
            return set()
        self.ensure_collection()
        existing: set[str] = set()
        for offset in range(0, len(ids), 1000):
            batch = list(ids[offset : offset + 1000])
            rows = self.client.query(
                collection_name=self._collection_name,
                ids=batch,
                output_fields=["id"],
            )
            existing.update(str(row["id"]) for row in rows)
        return existing

    def list_all_ids(self, *, batch_size: int = 1000) -> set[str]:
        """Return all live primary keys in Milvus for orphan reconciliation."""

        if not self.client.has_collection(collection_name=self._collection_name):
            return set()
        self.ensure_collection()
        iterator_fn = getattr(self.client, "query_iterator", None)
        if callable(iterator_fn):
            found: set[str] = set()
            iterator = iterator_fn(
                collection_name=self._collection_name,
                batch_size=batch_size,
                filter="",
                output_fields=["id"],
            )
            try:
                while batch := iterator.next():
                    found.update(str(row["id"]) for row in batch)
            finally:
                close_iter = getattr(iterator, "close", None)
                if callable(close_iter):
                    close_iter()
            return found

        found = set()
        last_id = ""
        while True:
            expr = f'id > "{last_id}"' if last_id else ""
            rows = self.client.query(
                collection_name=self._collection_name,
                filter=expr,
                output_fields=["id"],
                limit=batch_size,
            )
            if not rows:
                break
            batch_ids = sorted(str(row["id"]) for row in rows)
            new_ids = [item for item in batch_ids if item not in found]
            if not new_ids:
                break
            found.update(new_ids)
            if len(rows) < batch_size:
                break
            last_id = new_ids[-1]
        return found

    def ensure_collection(self, *, recreate_incompatible: bool = False) -> None:
        if self._ensured and not recreate_incompatible:
            return
        if self.client.has_collection(collection_name=self._collection_name):
            mismatches = self._incompatible_varchar_fields()
            if mismatches:
                drop_fn = getattr(self.client, "drop_collection", None)
                live_count = 0 if recreate_incompatible else self._raw_count()
                if callable(drop_fn) and (recreate_incompatible or live_count == 0):
                    logger.info(
                        "recreating Milvus collection '%s' to upgrade legacy VARCHAR limits: %s",
                        self._collection_name,
                        mismatches,
                    )
                    drop_fn(collection_name=self._collection_name)
                    self._ensured = False
                else:
                    logger.warning(
                        "Milvus collection '%s' uses legacy VARCHAR byte limits %s; "
                        "rebuild or re-sync the collection to avoid UTF-8 length overflows",
                        self._collection_name,
                        mismatches,
                    )
                    self.client.load_collection(collection_name=self._collection_name)
                    self._ensured = True
                    return
            else:
                self.client.load_collection(collection_name=self._collection_name)
                self._ensured = True
                return
        schema = self.client.create_schema(
            auto_id=False,
            enable_dynamic_field=False,
        )
        schema.add_field(
            field_name="id",
            datatype=DataType.VARCHAR,
            is_primary=True,
            max_length=MAX_ID_BYTES,
        )
        schema.add_field(
            field_name="text",
            datatype=DataType.VARCHAR,
            max_length=MAX_TEXT_BYTES,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
            enable_match=True,
        )
        schema.add_field(
            field_name="sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR,
        )
        schema.add_field(
            field_name="dense_vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=VECTOR_DIMENSION,
        )
        schema.add_field(
            field_name="category",
            datatype=DataType.VARCHAR,
            max_length=MAX_CATEGORY_BYTES,
        )
        schema.add_field(
            field_name="content_type",
            datatype=DataType.VARCHAR,
            max_length=MAX_CONTENT_TYPE_BYTES,
        )
        schema.add_field(
            field_name="section_path",
            datatype=DataType.VARCHAR,
            max_length=MAX_SECTION_PATH_BYTES,
        )
        schema.add_function(
            Function(
                name="text_bm25",
                function_type=FunctionType.BM25,
                input_field_names=["text"],
                output_field_names=["sparse_vector"],
            )
        )
        index_params = self.client.prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        self.client.create_collection(
            collection_name=self._collection_name,
            schema=schema,
            index_params=index_params,
        )
        self.client.load_collection(collection_name=self._collection_name)
        self._ensured = True

    def upsert(
        self,
        ids: Sequence[str],
        vectors: Sequence[Sequence[float]],
        texts: Sequence[str],
        metadata: Sequence[dict[str, str]],
    ) -> None:
        if not (len(ids) == len(vectors) == len(texts) == len(metadata)):
            raise ValueError("knowledge IDs, vectors, texts, and metadata must align")
        if any(len(vector) != VECTOR_DIMENSION for vector in vectors):
            raise ValueError("Milvus knowledge vectors must have 1024 dimensions")
        payload: list[dict[str, object]] = []
        for vector_id, vector, text, values in zip(ids, vectors, texts, metadata, strict=True):
            category = values.get("category", "")
            content_type = values.get("content_type", "")
            section_path = values.get("section_path", "")
            _check_utf8_limit("id", vector_id, MAX_ID_BYTES)
            _check_utf8_limit("text", text, MAX_TEXT_BYTES)
            _check_utf8_limit("category", category, MAX_CATEGORY_BYTES)
            _check_utf8_limit("content_type", content_type, MAX_CONTENT_TYPE_BYTES)
            _check_utf8_limit("section_path", section_path, MAX_SECTION_PATH_BYTES)
            payload.append(
                {
                    "id": vector_id,
                    "text": text,
                    "dense_vector": list(vector),
                    "category": category,
                    "content_type": content_type,
                    "section_path": section_path,
                }
            )
        self.client.upsert(
            collection_name=self._collection_name,
            data=payload,
        )
        # The caller marks these rows done in MySQL as soon as this method
        # returns. Flush first so a committed done row is searchable in Milvus.
        self.client.flush(collection_name=self._collection_name)

    def delete(self, ids: Sequence[str]) -> None:
        """Delete exact stable primary keys and flush so reads see deletions immediately."""

        if not ids or not self.client.has_collection(collection_name=self._collection_name):
            return
        self.client.delete(
            collection_name=self._collection_name,
            ids=list(ids),
        )
        flush_fn = getattr(self.client, "flush", None)
        if callable(flush_fn):
            flush_fn(collection_name=self._collection_name)

    def search_dense(
        self, vector: Sequence[float], *, limit: int, filter_expression: str = ""
    ) -> list[tuple[str, float]]:
        if len(vector) != VECTOR_DIMENSION:
            raise ValueError("Milvus query vectors must have 1024 dimensions")
        self.ensure_collection()
        results = self.client.search(
            collection_name=self._collection_name,
            data=[list(vector)],
            anns_field="dense_vector",
            search_params={"metric_type": "COSINE", "params": {}},
            limit=limit,
            filter=filter_expression,
        )
        return self._hits(results)

    def search_bm25(
        self, query: str, *, limit: int, filter_expression: str = ""
    ) -> list[tuple[str, float]]:
        self.ensure_collection()
        results = self.client.search(
            collection_name=self._collection_name,
            data=[query],
            anns_field="sparse_vector",
            search_params={"metric_type": "BM25", "params": {}},
            limit=limit,
            filter=filter_expression,
        )
        return self._hits(results)

    def search_hybrid(
        self,
        vector: Sequence[float],
        query: str,
        *,
        dense_limit: int,
        bm25_limit: int,
        limit: int,
        filter_expression: str = "",
    ) -> list[tuple[str, float]]:
        if len(vector) != VECTOR_DIMENSION:
            raise ValueError("Milvus query vectors must have 1024 dimensions")
        self.ensure_collection()
        dense = AnnSearchRequest(
            data=[list(vector)],
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=dense_limit,
            filter=filter_expression or None,
        )
        lexical = AnnSearchRequest(
            data=[query],
            anns_field="sparse_vector",
            param={"metric_type": "BM25", "params": {}},
            limit=bm25_limit,
            filter=filter_expression or None,
        )
        results = self.client.hybrid_search(
            collection_name=self._collection_name,
            reqs=[dense, lexical],
            ranker=RRFRanker(),
            limit=limit,
        )
        return self._hits(results)

    @staticmethod
    def _hits(results: list[list[dict[str, object]]]) -> list[tuple[str, float]]:
        if not results:
            return []
        return [
            (str(hit["id"]), float(hit["distance"]))
            for hit in results[0]
        ]


def supports_recreate_flag(store: object) -> bool:
    ensure_fn = getattr(store, "ensure_collection", None)
    if not callable(ensure_fn):
        return False
    try:
        return "recreate_incompatible" in inspect.signature(ensure_fn).parameters
    except (TypeError, ValueError):
        return False
