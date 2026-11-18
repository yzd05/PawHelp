"""Offline unit and integration tests for topics, RAG dual-write hardening, Alembic, and eval runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import Column, DateTime, Integer, JSON, MetaData, String, Table, Text, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.database.models import Conversation, KnowledgeChunk, Message, ReviewQueue
from app.database.session import MYSQL_UTC_INIT_COMMAND, create_async_engine_from_settings
from app.evaluation.retrieval_quality import (
    CachedQueryPlanner,
    EvalCase,
    group_retrieval_scores,
    reciprocal_rank,
)
from app.evaluation.runtime import run_retrieval_evaluation
from app.flywheel import (
    Flywheel,
    MAX_APPROVED_ANSWER_BYTES,
    flywheel_section_path,
    validate_approved_answer_bytes,
)
from app.knowledge.confidence import EvidenceGate
from app.knowledge.consistency import KnowledgeConsistencyChecker
from app.knowledge.embedding import VECTOR_DIMENSION
from app.knowledge.pipeline import KnowledgeEvidencePipeline, QuestionSplitter
from app.knowledge.query_understanding import IdentityQueryUnderstanding, QueryPlan
from app.knowledge.search import HybridKnowledgeSearch
from app.knowledge.vector_store import (
    MAX_CATEGORY_BYTES,
    MAX_SECTION_PATH_BYTES,
    MAX_TEXT_BYTES,
    MilvusKnowledgeStore,
)
from app.knowledge.vectorization import reconcile_missing_vectors, vectorize_pending
from app.repositories.knowledge import KnowledgeRepository
from app.repositories.knowledge_mining import KnowledgeMiningRepository
from app.schemas import ManualKnowledgeRequest, MarkdownKnowledgeRequest
from app.services.knowledge_admin import (
    KnowledgeAdminService,
    KnowledgeConflictError,
    dual_write_is_consistent,
)
from app.services.orders import OrderData
from app.topics.api import topic_router
from app.topics.data import PaidBudget, corpus_report, digest, exclusive_run, read_jsonl, redact, text_key, write_jsonl
from app.topics.metrics import evaluate, evaluate_sources, labels_from_scores, scan_threshold, sigmoid
from app.topics.reports import acceptance_reports
from app.topics.taxonomy import TAXONOMY_HASH, TOPIC_NAMES, terminology_table, validate_labels

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _make_settings(**overrides: object) -> Settings:
    defaults = {
        "_env_file": None,
        "chat_base_url": "http://localhost:11434/v1",
        "openai_model": "deepseek-chat",
        "openai_api_key": "test-key",
        "database_url": "mysql+asyncmy://user:pass@127.0.0.1:3306/pawhelp",
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# 1. Offline Topic Classifier Domain (app/topics/*)
# ---------------------------------------------------------------------------


def test_taxonomy_contracts_and_validation() -> None:
    assert len(TOPIC_NAMES) == 17
    assert TOPIC_NAMES[-1] == "其他"
    assert validate_labels(["物流", "退换货"]) == ["退换货", "物流"]
    assert validate_labels(["其他"]) == ["其他"]
    assert "退换货：" in terminology_table()

    for invalid in ([], "退换货", ["不存在类目"], ["物流", "物流"], ["物流", "其他"]):
        with pytest.raises(ValueError):
            validate_labels(invalid)


def test_redact_masks_all_nine_pii_patterns_and_preserves_semantics() -> None:
    sample = (
        "姓名：张三 收货地址：上海市浦东新区世纪大道100号2室；"
        "邮箱 foo.bar+1@example.com，身份证 31010119900307123X，"
        "电话 +86 13812345678，微信：wx_user_99，订单号: ORD-2026-8899，"
        "还有 100234 号订单 以及长数字 123456789012345 不能机洗吗？"
    )
    cleaned = redact(sample)
    assert "<姓名>" in cleaned and "张三" not in cleaned
    assert "<地址>" in cleaned and "世纪大道" not in cleaned
    assert "<邮箱>" in cleaned and "foo.bar" not in cleaned
    assert "<证件>" in cleaned and "3101011990" not in cleaned
    assert "<电话>" in cleaned and "13812345678" not in cleaned
    assert "<联系方式>" in cleaned and "wx_user_99" not in cleaned
    assert "订单号: <编号>" in cleaned and "ORD-2026-8899" not in cleaned
    assert "<编号>订单" in cleaned and "100234" not in cleaned
    assert "<长编号>" in cleaned and "123456789012345" not in cleaned
    assert "不能机洗吗" in cleaned
    assert text_key("订单号：ABCD1234，不能退吗？！") == text_key("订单号: XY9999 不能退吗")


def test_exclusive_run_blocks_live_pid_and_recovers_stale_dead_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "jobs" / "classify.lock"
    lock_path.parent.mkdir(parents=True)

    # Stale lock written by a dead PID must be automatically recovered.
    lock_path.write_text("999999999", encoding="utf-8")
    with exclusive_run(lock_path):
        assert lock_path.exists()
        assert int(lock_path.read_text(encoding="utf-8")) == os.getpid()
        # Re-entering while held by current live PID must raise FileExistsError.
        with pytest.raises(FileExistsError):
            with exclusive_run(lock_path):
                pass
    assert not lock_path.exists()


def test_paid_budget_reserves_finishes_and_enforces_hard_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PaidBudget(tmp_path / "bad.json", limit=101)
    with pytest.raises(ValueError):
        PaidBudget(tmp_path / "zero.json", limit=0)

    budget_file = tmp_path / "budget.json"
    # Pre-seed a stale lock to prove PaidBudget recovers from crashed runs too.
    budget_file.with_suffix(".lock").write_text("999999999", encoding="utf-8")

    budget = PaidBudget(budget_file, limit=2)
    c1 = budget.reserve("prelabel", {"q": "问题1"})
    budget.finish(c1, status="completed", usage={"input_tokens": 12, "output_tokens": 4})
    c2 = budget.reserve("prelabel", {"q": "问题2"})
    assert (c1, c2) == ("1", "2")

    with pytest.raises(RuntimeError, match="exhausted"):
        budget.reserve("prelabel", {"q": "超预算"})
    with pytest.raises(ValueError, match="cannot reset/raise"):
        PaidBudget(budget_file, limit=3).reserve("prelabel", {"q": "篡改上限"})

    rows_path = tmp_path / "corpus.jsonl"
    write_jsonl(rows_path, [{"text": "退货", "labels": ["退换货"]}, {"text": "坏标签", "labels": ["未知"]}])
    loaded = read_jsonl(rows_path)
    report = corpus_report(loaded)
    assert report["size"] == 2
    assert report["label_counts"]["退换货"] == 1
    assert len(report["invalid"]) == 1


def test_topic_metrics_and_threshold_selection() -> None:
    assert sigmoid(0.0) == pytest.approx(0.5)
    assert sigmoid(500.0) == pytest.approx(1.0)
    assert sigmoid(-500.0) == pytest.approx(0.0)

    zeros = [0.1] * len(TOPIC_NAMES)
    assert labels_from_scores(zeros, 0.5) == ["其他"]

    multi = [0.1] * len(TOPIC_NAMES)
    multi[0] = 0.85  # 退换货
    multi[1] = 0.65  # 物流
    multi[-1] = 0.99  # 其他 is ignored when a specific label crosses threshold
    assert labels_from_scores(multi, 0.5) == ["退换货", "物流"]

    with pytest.raises(ValueError):
        labels_from_scores([0.5], 0.5)
    with pytest.raises(ValueError):
        labels_from_scores(zeros, 1.0)

    expected = [["退换货"], ["物流", "运费"], ["其他"]]
    predicted = [["退换货"], ["物流"], ["其他"]]
    summary = evaluate(expected, predicted)
    assert summary["size"] == 3
    assert summary["exact_match"] == pytest.approx(2 / 3)
    assert summary["errors"] == {"false_positive_labels": 0, "false_negative_labels": 1, "wrong_samples": 1}

    by_source = evaluate_sources(expected, predicted, ["real", "synthetic", "real"])
    assert by_source["real"]["exact_match"] == 1.0
    assert by_source["synthetic"]["exact_match"] == 0.0

    score_rows = [multi, [0.7 if i in (1, 5) else 0.05 for i in range(len(TOPIC_NAMES))], zeros]
    scanned = scan_threshold(expected, score_rows)
    assert scanned["selection_split"] == "validation_only"
    assert scanned["chosen"]["micro_f1"] == 1.0
    assert scanned["chosen"]["threshold"] == 0.7  # filters out 0.65 FP on row 0 and keeps 0.7 TP on row 1


def test_acceptance_reports_projection_staleness_and_redaction(tmp_path: Path) -> None:
    data_root = tmp_path / "data" / "topic-classifier"
    (data_root / "reports" / "batches").mkdir(parents=True)
    (data_root / "dataset").mkdir(parents=True)
    (data_root / "model").mkdir(parents=True)
    (tmp_path / "config").mkdir(parents=True)

    manifest = {
        "before_augmentation": {s: {"size": 10} for s in ("train", "val", "test")},
        "after_augmentation": {s: {"size": 12} for s in ("train", "val", "test")},
        "overlaps": {"train_test": {"count": 0}},
    }
    (data_root / "dataset" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    eval_metrics = evaluate([["退换货"], ["物流"]], [["退换货"], ["运费"]])
    eval_payload = {
        **eval_metrics,
        "model_version": "v1",
        "threshold": 0.5,
        "taxonomy_hash": TAXONOMY_HASH,
        "test_sha256": "test-hash-1",
    }
    (data_root / "reports" / "eval-report.json").write_text(json.dumps(eval_payload), encoding="utf-8")
    (data_root / "model" / "threshold.json").write_text(
        json.dumps(
            {
                "model_version": "v1",
                "threshold": 0.5,
                "taxonomy_hash": TAXONOMY_HASH,
                "labels": list(TOPIC_NAMES),
                "manifest_hash": digest(manifest),
                "dataset_hashes": {"test": "test-hash-1"},
            }
        ),
        encoding="utf-8",
    )
    (data_root / "model" / "threshold-scan.json").write_text(
        json.dumps(
            {
                "selection_split": "validation_only",
                "chosen": {"threshold": 0.5, "micro_f1": 0.8, "macro_f1": 0.7, "fp": 1, "fn": 1},
                "trials": [{"threshold": 0.5, "micro_f1": 0.8, "macro_f1": 0.7, "fp": 1, "fn": 1}],
                "validation_scores": [[0.1] * len(TOPIC_NAMES)],
            }
        ),
        encoding="utf-8",
    )
    (data_root / "reports" / "error-samples.json").write_text(
        json.dumps(
            [
                {
                    "text": "手机号 13912345678 快递到哪了",
                    "expected": ["物流"],
                    "predicted": ["运费"],
                    "missed": ["物流"],
                    "extra": ["运费"],
                    "scores": {"物流": 0.4},
                }
            ]
        ),
        encoding="utf-8",
    )
    (data_root / "golden-predictions.report.json").write_text(
        json.dumps({"size": 5, "exact_match": 1.0, "gate_passed": True, "taxonomy_hash": TAXONOMY_HASH}),
        encoding="utf-8",
    )
    (data_root / "reports" / "batches" / "classify-001.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "started_at": "2026-10-13T00:00:00Z",
                "finished_at": "2026-10-13T00:01:00Z",
                "model_version": "v1",
                "pending": 2,
                "classified": [{"status": "saved"}, {"status": "saved"}],
                "failed": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config" / "topic-acceptance.json").write_text(
        json.dumps(
            {
                "strict": {"labels": list(TOPIC_NAMES[:5]), "minimum_f1": 0.85},
                "standard": {"labels": list(TOPIC_NAMES[5:16]), "minimum_f1": 0.75},
                "observe": {"labels": [TOPIC_NAMES[16]], "minimum_f1": None},
            }
        ),
        encoding="utf-8",
    )

    projected = acceptance_reports(tmp_path)
    assert projected["evaluation"]["present"] is True
    assert projected["evaluation"]["stale"] is False
    assert projected["errors"]["stale"] is False
    assert "<电话>" in projected["errors"]["data"][0]["text"]
    assert "13912345678" not in projected["errors"]["data"][0]["text"]
    assert "validation_scores" not in projected["threshold"]["data"]
    assert projected["threshold"]["validation_size"] == 1
    assert projected["acceptance_policy"]["present"] is True
    assert projected["batch"] == {
        "present": True,
        "status": "completed",
        "started_at": "2026-10-13T00:00:00Z",
        "finished_at": "2026-10-13T00:01:00Z",
        "model_version": "v1",
        "pending": 2,
        "saved": 2,
        "failed": 0,
        "path": "reports/batches/classify-001.json",
    }


@pytest.mark.asyncio
async def test_topic_router_admin_token_local_header_and_configurable_service_url(
    monkeypatch, tmp_path: Path
) -> None:
    static_dir = tmp_path / "app" / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "topics.html").write_text("<html>topics</html>", encoding="utf-8")

    now = datetime(2026, 10, 20, 12, 0, 0)
    fake_rows = [
        {
            "id": 10,
            "raw_question": "手机号 13800138000 退货怎么办理",
            "normalized_question": "13800138000 七天无理由退货流程",
            "source": "retrieval_low_conf",
            "created_at": now,
            "matched_review_id": 3,
            "occurrence_count": 4,
            "review_status": "待审",
            "labels": ["退换货"],
            "classified_at": now,
        },
        {
            "id": 11,
            "raw_question": "未合并原始追问",
            "normalized_question": None,
            "source": "user_feedback",
            "created_at": now,
            "matched_review_id": None,
            "occurrence_count": None,
            "review_status": None,
            "labels": ["退换货"],
            "classified_at": now,
        },
    ]

    async def fake_questions(self, *, pending: bool = False, limit=None):
        return fake_rows

    monkeypatch.setattr("app.topics.pool.TopicPool.questions", fake_questions)

    requested_urls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url: str):
            requested_urls.append(url)
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "status": "ok",
                    "model_version": "bert-topic-v2",
                    "taxonomy_hash": TAXONOMY_HASH,
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    settings = SimpleNamespace(admin_token=SecretStr(""), topic_service_url="http://127.0.0.1:19110")
    app = FastAPI()
    app.state.settings = settings
    app.include_router(topic_router(None, tmp_path))

    # 1) Remote client without token -> 503 when ADMIN_TOKEN is unset; 401/200 when ADMIN_TOKEN is configured.
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.1.25", 4321)),
        base_url="http://localhost:8000",
    ) as remote:
        assert (await remote.get("/topics")).status_code == 200
        assert (await remote.get("/api/topics/distribution")).status_code == 503
        assert (
            await remote.get("/api/topics/distribution", headers={"X-PawHelp-Local-Admin": "1"})
        ).status_code == 403

        settings.admin_token = SecretStr("secret-topic-admin")
        assert (await remote.get("/api/topics/distribution")).status_code == 401
        assert (
            await remote.get("/api/topics/distribution", headers={"X-Admin-Token": "wrong"})
        ).status_code == 401
        # Dual-header console request from LAN/container: X-Admin-Token takes precedence over X-PawHelp-Local-Admin!
        assert (
            await remote.get(
                "/api/topics/distribution",
                headers={"X-PawHelp-Local-Admin": "1", "X-Admin-Token": "wrong"},
            )
        ).status_code == 401
        dual_ok = await remote.get(
            "/api/topics/distribution",
            headers={"X-PawHelp-Local-Admin": "1", "X-Admin-Token": "secret-topic-admin"},
        )
        assert dual_ok.status_code == 200
        ok_dist = await remote.get(
            "/api/topics/distribution", headers={"X-Admin-Token": "secret-topic-admin"}
        )
        assert ok_dist.status_code == 200
        assert ok_dist.json()["total"] == 2
        assert ok_dist.json()["taxonomy_hash"] == TAXONOMY_HASH

        # Service health endpoint uses configured topic_service_url
        svc = await remote.get(
            "/api/acceptance/service", headers={"X-Admin-Token": "secret-topic-admin"}
        )
        assert svc.status_code == 200 and svc.json()["online"] is True
        assert requested_urls == ["http://127.0.0.1:19110/healthz"]

    # 2) Loopback same-origin works with X-PawHelp-Local-Admin: 1 when no X-Admin-Token is sent,
    #    and explicitly rejects a forged X-Admin-Token even on loopback.
    local_headers = {
        "X-PawHelp-Local-Admin": "1",
        "Origin": "http://localhost:8000",
        "Sec-Fetch-Site": "same-origin",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 4321)),
        base_url="http://localhost:8000",
    ) as local:
        assert (
            await local.get(
                "/api/topics/distribution",
                headers={**local_headers, "X-Admin-Token": "wrong"},
            )
        ).status_code == 401
        settings.admin_token = SecretStr("")
        assert (await local.get("/api/topics/questions?label=不存在", headers=local_headers)).status_code == 400
        q_resp = await local.get(
            "/api/topics/questions?label=退换货&review_status=待审&q=13800138000",
            headers=local_headers,
        )
        assert q_resp.status_code == 200
        payload = q_resp.json()
        assert payload["total"] == 1
        assert "<电话>" in payload["items"][0]["text"]
        assert "13800138000" not in payload["items"][0]["raw_text"]


# ---------------------------------------------------------------------------
# 2. Milvus Schema, Poison-Pill Isolation, Tombstones & Flywheel Hardening
# ---------------------------------------------------------------------------


def test_milvus_schema_byte_limits_legacy_recreate_and_delete_flush() -> None:
    class SchemaField:
        def __init__(self) -> None:
            self.fields: dict[str, int] = {}

        def add_field(self, *, field_name: str, max_length: int | None = None, **kwargs: object) -> None:
            if max_length is not None:
                self.fields[field_name] = max_length

        def add_function(self, *args: object, **kwargs: object) -> None:
            pass

    class IndexParams:
        def add_index(self, **kwargs: object) -> None:
            pass

    class RecordingMilvusClient:
        def __init__(self, *, has_existing: bool = True, live_rows: int = 0) -> None:
            self.has_existing = has_existing
            self.live_rows = live_rows
            self.events: list[str] = []
            self.schema = SchemaField()

        def has_collection(self, *, collection_name: str) -> bool:
            return self.has_existing

        def describe_collection(self, *, collection_name: str) -> dict[str, object]:
            return {
                "fields": [
                    {"name": "category", "params": {"max_length": 255}},
                    {"name": "section_path", "params": {"max_length": 512}},
                    {"name": "text", "params": {"max_length": 8192}},
                ]
            }

        def query(self, *, collection_name: str, output_fields: list[str], **kwargs: object):
            return [{"count(*)": self.live_rows}]

        def drop_collection(self, *, collection_name: str) -> None:
            self.events.append("drop")
            self.has_existing = False

        def create_schema(self, **kwargs: object) -> SchemaField:
            return self.schema

        def prepare_index_params(self) -> IndexParams:
            return IndexParams()

        def create_collection(self, **kwargs: object) -> None:
            self.events.append("create")
            self.has_existing = True

        def load_collection(self, *, collection_name: str) -> None:
            self.events.append("load")

        def delete(self, *, collection_name: str, ids: list[str]) -> None:
            self.events.append(f"delete:{','.join(ids)}")

        def flush(self, *, collection_name: str) -> None:
            self.events.append("flush")

    # Empty legacy collection is dropped and recreated with expanded UTF-8 byte capacities.
    store = MilvusKnowledgeStore("http://milvus.invalid")
    client = RecordingMilvusClient(has_existing=True, live_rows=0)
    store._client = client  # type: ignore[assignment]
    store.ensure_collection()

    assert "drop" in client.events and "create" in client.events
    assert client.schema.fields["category"] == MAX_CATEGORY_BYTES == 1024
    assert client.schema.fields["section_path"] == MAX_SECTION_PATH_BYTES == 2048
    assert client.schema.fields["text"] == MAX_TEXT_BYTES == 65535

    # delete() explicitly flushes for read-after-delete consistency.
    client.events.clear()
    store.delete(["42", "43"])
    assert client.events == ["delete:42,43", "flush"]

    # Oversized UTF-8 text raises ValueError deterministically before hitting Milvus RPC.
    with pytest.raises(ValueError, match="exceeds max_length"):
        store.upsert(
            ["1"],
            [[0.0] * VECTOR_DIMENSION],
            ["超" * 25000],  # 75,000 UTF-8 bytes > 65,535
            [{"category": "售后", "content_type": "faq", "section_path": "退货"}],
        )


@pytest.mark.asyncio
async def test_vectorize_pending_isolates_poison_pill_and_reconcile_cleans_tombstones_and_orphans() -> None:
    rows = [
        SimpleNamespace(
            id=1,
            category="售后",
            questions="毒丸条目",
            answer="毒" * 25000,
            section_path="售后 / 毒丸",
            content_type="faq",
            vectorize_status="pending",
            vector_id=None,
        ),
        SimpleNamespace(
            id=2,
            category="售后",
            questions="正常条目2",
            answer="签收七天无理由退货",
            section_path="售后 / 退货",
            content_type="faq",
            vectorize_status="pending",
            vector_id=None,
        ),
        SimpleNamespace(
            id=3,
            category="售后",
            questions="正常条目3",
            answer="运费险理赔规则",
            section_path="售后 / 运费",
            content_type="faq",
            vectorize_status="pending",
            vector_id=None,
        ),
        SimpleNamespace(
            id=4,
            category="旧块",
            questions="半删墓碑块",
            answer="不应复活",
            section_path="旧",
            content_type="faq",
            vectorize_status="deleting",
            vector_id=None,
        ),
    ]

    class InMemoryRepo:
        async def list_pending(self, *, limit: int):
            return [r for r in rows if r.vectorize_status == "pending"][:limit]

        async def list_deleting(self, *, limit: int):
            return [r for r in rows if r.vectorize_status == "deleting"][:limit]

        async def delete_ids(self, ids):
            rows[:] = [r for r in rows if r.id not in set(ids)]

        async def mark_vectorized(self, batch) -> None:
            for r in batch:
                r.vector_id = str(r.id)
                r.vectorize_status = "done"

        async def mark_failed(self, batch) -> None:
            for r in batch:
                r.vector_id = None
                r.vectorize_status = "failed"

        async def mark_pending(self, batch) -> None:
            for r in batch:
                r.vector_id = None
                r.vectorize_status = "pending"

        async def list_vectorized_after_id(self, *, after_id: int, limit: int):
            return [r for r in rows if r.id > after_id and r.vectorize_status == "done"][:limit]

    class FakeMilvus:
        def __init__(self) -> None:
            self.vectors: dict[str, list[float]] = {"4": [0.1] * VECTOR_DIMENSION, "999": [0.2] * VECTOR_DIMENSION}

        def ensure_collection(self, **kwargs: object) -> None:
            pass

        def upsert(self, ids, vectors, texts, metadata) -> None:
            for vid, vec, txt in zip(ids, vectors, texts, strict=True):
                if len(txt.encode("utf-8")) > MAX_TEXT_BYTES:
                    raise ValueError("Milvus text byte limit exceeded")
                self.vectors[vid] = list(vec)

        def existing_ids(self, ids):
            return set(ids) & set(self.vectors)

        def list_all_ids(self):
            return set(self.vectors)

        def delete(self, ids):
            for item in ids:
                self.vectors.pop(item, None)

    repo = InMemoryRepo()
    store = FakeMilvus()
    commits = 0

    async def commit() -> None:
        nonlocal commits
        commits += 1

    # Batch size 2: [poison #1, healthy #2] triggers single-row fallback, then [#3] succeeds,
    # and deleting tombstone #4 is NEVER resurrected!
    completed = await vectorize_pending(
        repo,
        SimpleNamespace(embed=lambda texts: [[0.0] * VECTOR_DIMENSION for _ in texts]),
        store,
        commit,
        batch_size=2,
    )
    assert completed == 2
    assert [(r.id, r.vectorize_status) for r in rows] == [
        (1, "failed"),
        (2, "done"),
        (3, "done"),
        (4, "deleting"),
    ]
    assert dual_write_is_consistent({"failed": 1, "done": 2, "deleting": 1}, len(store.vectors)) is False

    # Bidirectional reconciliation deletes tombstone #4 (both Milvus & MySQL) AND orphan "999" in Milvus!
    repaired = await reconcile_missing_vectors(repo, store, commit, batch_size=2)
    assert repaired == 2  # tombstone #4 + orphan #999
    assert [r.id for r in rows] == [1, 2, 3]
    assert set(store.vectors) == {"2", "3"}


@pytest.mark.asyncio
async def test_flywheel_section_path_truncation_byte_guard_and_deleted_chunk_recovery() -> None:
    long_question = "猫" * 512
    path = flywheel_section_path(long_question)
    assert len(path) <= 512
    assert len(path.encode("utf-8")) <= 2000

    with pytest.raises(ValueError, match="UTF-8"):
        validate_approved_answer_bytes("普通问题", "答" * (MAX_APPROVED_ANSWER_BYTES // 3 + 10))

    # Build an in-memory SQLite database for ReviewQueue + KnowledgeChunk recovery testing.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    meta = MetaData()
    Table(
        "knowledge_chunks",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("category", String(255)),
        Column("questions", Text),
        Column("answer", Text),
        Column("section_path", String(512)),
        Column("content_type", String(32)),
        Column("source_kind", String(24), server_default="manual"),
        Column("source_path", String(512)),
        Column("is_key_clause", Integer, server_default="0"),
        Column("prev_chunk_id", Integer),
        Column("next_chunk_id", Integer),
        Column("vector_id", String(64)),
        Column("vectorize_status", String(24), server_default="pending"),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        sqlite_autoincrement=True,
    )
    Table(
        "review_queue",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("normalized_question", String(512)),
        Column("ai_suggested_answer", Text),
        Column("occurrence_count", Integer, server_default="1"),
        Column("review_status", String(16), server_default="待审"),
        Column("approved_answer", Text),
        Column("published_chunk_id", Integer),
        Column("publication_error", Text),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        sqlite_autoincrement=True,
    )
    Table(
        "low_confidence_questions",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("conversation_id", Integer),
        Column("raw_question", Text),
        Column("source", String(32)),
        Column("reason", Text),
        Column("retrieved_chunks", JSON),
        Column("matched_review_id", Integer),
        Column("event_key", String(128)),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(meta.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        class FakeKnowledge:
            async def assert_consistent(self, q, a, **kwargs):
                return None

            async def vectorize_pending(self):
                async with sessions() as s, s.begin():
                    pending = list(
                        await s.scalars(
                            select(KnowledgeChunk).where(KnowledgeChunk.vectorize_status == "pending")
                        )
                    )
                    for row in pending:
                        row.vector_id = str(row.id)
                        row.vectorize_status = "done"

        flywheel = Flywheel(
            _make_settings(),
            sessions,
            SimpleNamespace(max_retries=0),
            FakeKnowledge(),
        )
        async with sessions() as s, s.begin():
            rq = ReviewQueue(normalized_question=long_question, review_status="待审", occurrence_count=1)
            s.add(rq)
            await s.flush()
            review_id = rq.id

        approved = await flywheel.review(review_id, "核准回收说明。")
        first_chunk_id = approved["published_chunk_id"]
        assert approved["publication_status"] == "已同步"

        # Simulate operator deleting the published chunk in Knowledge Console (SET NULL).
        async with sessions() as s, s.begin():
            await KnowledgeRepository(s).delete_ids([first_chunk_id])
            rq = await s.get(ReviewQueue, review_id)
            rq.published_chunk_id = None

        before_recover = await flywheel.detail(review_id)
        assert before_recover["publication_status"] == "待同步"

        # Clicking publish() recreates the missing KnowledgeChunk and syncs cleanly without 409 deadlock!
        recovered = await flywheel.publish(review_id)
        assert recovered["publication_status"] == "已同步"
        assert recovered["published_chunk_id"] is not None
        assert recovered["published_chunk_id"] != first_chunk_id
        assert recovered["publication_error"] is None

        # Deleting the recreated chunk also allows review() to re-approve with revised text instead of 409 deadlock!
        second_chunk_id = recovered["published_chunk_id"]
        async with sessions() as s, s.begin():
            await KnowledgeRepository(s).delete_ids([second_chunk_id])
            rq = await s.get(ReviewQueue, review_id)
            rq.published_chunk_id = None
        reapproved = await flywheel.review(review_id, "二次修订核准说明。")
        assert reapproved["publication_status"] == "已同步"
        assert reapproved["approved_answer"] == "二次修订核准说明。"
        assert reapproved["published_chunk_id"] != second_chunk_id
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. EvidenceGate Fingerprint Decoupling, mtime Cache & Multi-Query Reranking
# ---------------------------------------------------------------------------


def test_committed_evidence_calibration_works_across_any_chat_model_and_caches_disk_reads(
    monkeypatch,
) -> None:
    evidence = [{"answer": "满 99 元包邮", "score": 0.6}]
    for model_name, method in (
        ("deepseek-chat", "json_mode"),
        ("deepseek-v4-flash", "json_mode"),
        ("gpt-4o", "json_schema"),
        ("qwen-max", "function_calling"),
    ):
        settings = _make_settings(
            openai_model=model_name,
            structured_output_method=method,
            evidence_calibration_path=str(PROJECT_ROOT / "config" / "evidence-calibration.json"),
        )
        gate = EvidenceGate(settings, PROJECT_ROOT / "knowledge")
        decision = gate.check(evidence, legacy_confident=False)
        assert decision.calibrated is True, model_name
        assert decision.threshold == pytest.approx(0.174)
        assert decision.accepted is True

    # Verify st_mtime cache avoids re-reading disk on subsequent checks.
    read_calls = 0
    real_read_text = Path.read_text

    def counting_read_text(self: Path, *args, **kwargs) -> str:
        nonlocal read_calls
        read_calls += 1
        return real_read_text(self, *args, **kwargs)

    settings = _make_settings(
        evidence_calibration_path=str(PROJECT_ROOT / "config" / "evidence-calibration.json")
    )
    gate = EvidenceGate(settings, PROJECT_ROOT / "knowledge")
    monkeypatch.setattr(Path, "read_text", counting_read_text)
    assert gate.check(evidence, legacy_confident=False).calibrated is True
    assert gate.check(evidence, legacy_confident=False).calibrated is True
    assert gate.check([], legacy_confident=False).calibrated is True
    assert read_calls == 1


@pytest.mark.asyncio
async def test_question_splitter_skips_bare_conjunctions_and_pipeline_deduplicates_rerank() -> None:
    calls: list[str] = []

    class CountingModel:
        def with_structured_output(self, schema, *, method):
            return self

        async def ainvoke(self, messages):
            calls.append(str(messages[-1].content))
            return {"queries": ["拆封猫砂还能退吗？", "人工客服入口在哪里？"]}

    splitter = QuestionSplitter(CountingModel(), method="json_mode")
    for single in ("加热垫和猫爬架怎么保修", "退货与换货区别", "你好，怎么退货", "猫窝和猫抓板是什么材质？"):
        assert await splitter.split(single) == [single]
    assert calls == []

    compound = "拆封猫砂还能退吗，哪里找人工客服？"
    assert await splitter.split(compound) == ["拆封猫砂还能退吗？", "人工客服入口在哪里？"]
    assert calls == [compound]

    # Build 30 hydrated chunks to verify:
    # 1) single query caps reranker input to rerank_input_limit=20 (not 30)
    # 2) multi-query gather() deduplicates chunk IDs across subqueries and calls reranker ONCE!
    chunks = {
        str(i): KnowledgeChunk(
            id=i,
            category="售后",
            questions=f"问题 {i}",
            answer=f"答案 {i}",
            section_path=f"章节 {i}",
            content_type="faq",
            vector_id=str(i),
            vectorize_status="done",
        )
        for i in range(1, 31)
    }

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalars(self, _stmt):
            return list(chunks.values())

    embed_batches: list[list[str]] = []
    rerank_batches: list[list[int]] = []

    class CountingEmbedder:
        def embed(self, texts):
            embed_batches.append(list(texts))
            return [[0.1] * VECTOR_DIMENSION for _ in texts]

    class CountingReranker:
        def rerank(self, query, rows, *, limit):
            rerank_batches.append([r.id for r in rows])
            return [SimpleNamespace(chunk=r, score=0.9 - idx * 0.01) for idx, r in enumerate(rows[:limit])]

    class OverlappingStore:
        def search_hybrid(self, vector, query, **kwargs):
            if "人工" in query:
                return [(str(i), 0.9) for i in (10, 1, 2, 11, 12)]
            return [(str(i), 0.9) for i in range(1, 31)]

    search = HybridKnowledgeSearch(
        lambda: DummySession(),  # type: ignore[arg-type]
        CountingEmbedder(),  # type: ignore[arg-type]
        OverlappingStore(),  # type: ignore[arg-type]
        CountingReranker(),  # type: ignore[arg-type]
        IdentityQueryUnderstanding(),
        rerank_limit=10,
        rerank_input_limit=20,
    )

    single_res = await search.retrieve("普通单句问题", limit=5)
    assert len(single_res.matches) == 5
    assert len(rerank_batches[-1]) == 20  # capped at rerank_input_limit=20 instead of 30

    embed_batches.clear()
    rerank_batches.clear()
    pipeline = KnowledgeEvidencePipeline(
        search,
        SimpleNamespace(check=lambda ev, **_: SimpleNamespace(accepted=True, calibrated=True, signals={}, threshold=0.174, reason="ok")),
        SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(useful=True, reason="ok", check_failed=False))),
        splitter,
        top_k=5,
    )
    merged = await pipeline.retrieve(compound)
    assert len(embed_batches) == 1 and len(embed_batches[0]) == 3  # 2 subquestions + original query batched once
    assert len(rerank_batches) == 1  # single converged rerank call!
    assert len(rerank_batches[0]) == len(set(rerank_batches[0])) <= 20  # zero duplicate chunk IDs!
    # Chunk 10 (top hit of subquery 2) is reserved in final Top-5 alongside subquery 1's top hits.
    assert 10 in [m.chunk.id for m in merged.result.matches]


# ---------------------------------------------------------------------------
# 4. Consistency Checks, Active-Session Mining Filter, UTC, Orders & Alembic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_admin_intra_batch_conflict_overview_file_count_and_sqlite_mining(
    tmp_path: Path,
) -> None:
    policies_dir = tmp_path / "policies"
    policies_dir.mkdir()
    bad_md = policies_dir / "corrupt-if-chunked.md"
    bad_md.write_text("# 政策\n\n正常文本", encoding="utf-8")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    meta = MetaData()
    Table(
        "conversations",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", String(64)),
        Column("status", String(16), server_default="进行中"),
        Column("summary", Text),
        Column("summary_upto_msg_id", Integer),
        Column("layer1_from_msg_id", Integer),
        Column("title", String(32)),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        sqlite_autoincrement=True,
    )
    Table(
        "messages",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("conversation_id", Integer),
        Column("role", String(16)),
        Column("content", Text),
        Column("tool_calls", JSON),
        Column("tool_call_id", String(64)),
        Column("tool_trace", JSON),
        Column("suggested_actions", JSON),
        Column("source_refs", JSON),
        Column("related_order_id", String(64)),
        Column("reply_to_message_id", Integer),
        Column("retrieved_chunks", JSON),
        Column("feedback", String(8)),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    )
    Table(
        "knowledge_chunks",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("category", String(255)),
        Column("questions", Text),
        Column("answer", Text),
        Column("section_path", String(512)),
        Column("content_type", String(32)),
        Column("source_kind", String(24), server_default="manual"),
        Column("source_path", String(512)),
        Column("is_key_clause", Integer, server_default="0"),
        Column("prev_chunk_id", Integer),
        Column("next_chunk_id", Integer),
        Column("vector_id", String(64)),
        Column("vectorize_status", String(24), server_default="pending"),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        sqlite_autoincrement=True,
    )
    Table(
        "qa_extraction_staging",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("batch_no", String(64)),
        Column("source_ref", String(255)),
        Column("question", Text),
        Column("answer", Text),
        Column("status", String(16), server_default="extracted"),
        Column("discard_reason", String(500)),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    )

    try:
        async with engine.begin() as conn:
            await conn.run_sync(meta.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        # Empty Milvus search: consistency checker must still catch unvectorized pending chunks
        # AND intra-batch conflicting candidates!
        class EmptySearch:
            async def retrieve(self, query, **kwargs):
                return SimpleNamespace(matches=[])

        class ConflictModel:
            def with_structured_output(self, schema, **kwargs):
                return self

            async def ainvoke(self, messages):
                payload = json.loads(messages[-1].content.split("\n", 1)[1])
                existing = payload["existing"]
                return {"verdict": "conflict", "existing_id": existing[0]["id"], "reason": "包邮门槛矛盾"}

        checker = KnowledgeConsistencyChecker(EmptySearch(), ConflictModel(), method="json_mode")
        stored_vectors: dict[str, list[float]] = {}
        store = SimpleNamespace(
            ensure_collection=lambda **_: None,
            upsert=lambda ids, vecs, txts, meta: stored_vectors.update(zip(ids, vecs, strict=True)),
            delete=lambda ids: [stored_vectors.pop(i, None) for i in ids],
            count_entities=lambda: len(stored_vectors),
            collection_name="knowledge_hybrid",
        )
        embedder = SimpleNamespace(embed=lambda texts: [[0.1] * VECTOR_DIMENSION for _ in texts])
        service = KnowledgeAdminService(
            sessions,
            embedder,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            EmptySearch(),  # type: ignore[arg-type]
            documents_root=tmp_path,
            vectorize_batch_size=16,
            consistency_checker=checker,
        )

        # create_manual synchronously vectorizes the new manual entry.
        created = await service.create_manual(
            ManualKnowledgeRequest(category="运费", questions="猫砂多少包邮", answer="满 99 元包邮。")
        )
        assert created.vectorize_status == "done"
        assert str(created.id) in stored_vectors

        # Even if a row is still pending (unvectorized), assert_consistent sees it via list_unvectorized!
        async with sessions() as s, s.begin():
            s.add(
                KnowledgeChunk(
                    category="洗护",
                    questions="猫窝可以机洗吗",
                    answer="可以冷水轻柔机洗。",
                    content_type="faq",
                    source_kind="manual",
                    vectorize_status="pending",
                )
            )
        with pytest.raises(KnowledgeConflictError):
            await service.assert_consistent("猫窝能机洗吗", "严禁机洗，只能干洗。")

        # Intra-batch conflict inside ingest_markdown: second section conflicts with first section in same upload!
        ingest_res = await service.ingest_markdown(
            MarkdownKnowledgeRequest(
                markdown="# 充电说明\n\n## 猫砂盆续航时间\n\n续航为 15 天。\n\n## 猫砂盆续航多久\n\n续航只有 3 天。",
                content_type="faq",
                vectorize_after=False,
            )
        )
        assert ingest_res.inserted == 1
        assert ingest_res.skipped_conflicts == 1

        # overview() counts files without reading or chunking Markdown contents.
        ov = await service.overview()
        assert ov.materials == 1

        # Conversation mining skips active short "进行中" conversation, mines "已结束" conversation.
        async with sessions() as s, s.begin():
            active_conv = Conversation(user_id="u1", status="进行中")
            ended_conv = Conversation(user_id="u2", status="已结束")
            s.add_all([active_conv, ended_conv])
            await s.flush()
            s.add_all(
                [
                    Message(conversation_id=active_conv.id, role="user", content="在吗"),
                    Message(conversation_id=active_conv.id, role="assistant", content="您好"),
                    Message(conversation_id=ended_conv.id, role="user", content="发票几天开出"),
                    Message(conversation_id=ended_conv.id, role="assistant", content="签收后 3 个工作日内开具电子发票。"),
                ]
            )
            ended_id = ended_conv.id

        async with sessions() as s:
            mining_repo = KnowledgeMiningRepository(s)
            stats = await mining_repo.stats()
            assert stats["unprocessed_conversations"] == 1
            transcripts = await mining_repo.load_unstaged_transcripts(limit=10)
            assert [t.conversation_id for t in transcripts] == [ended_id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mysql_utc_connect_arg_and_order_data_single_transaction_cache(monkeypatch) -> None:
    captured_kwargs: list[tuple[str, dict[str, object]]] = []

    def fake_create_async_engine(url: str, **kwargs: object):
        captured_kwargs.append((url, kwargs))
        return SimpleNamespace(url=url, kwargs=kwargs)

    monkeypatch.setattr("app.database.session.create_async_engine", fake_create_async_engine)
    create_async_engine_from_settings(_make_settings())
    assert captured_kwargs[-1][1]["connect_args"] == {"init_command": MYSQL_UTC_INIT_COMMAND}

    create_async_engine_from_settings(_make_settings(database_url="sqlite+aiosqlite:///:memory:"))
    assert captured_kwargs[-1][1]["connect_args"] == {}

    # OrderData caches initialized user_id so subsequent reads open 1 transaction instead of 2.
    begins = 0
    seed_calls = 0

    class Tx:
        async def __aenter__(self):
            nonlocal begins
            begins += 1
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return Tx()

    class FakeSessionFactory:
        def __call__(self):
            return FakeSession()

    class FakeOrderRepo:
        def __init__(self, _session):
            pass

        async def seed_demo(self, user_id: str) -> bool:
            nonlocal seed_calls
            seed_calls += 1
            return True

        async def list_owned(self, user_id: str):
            return [{"order_id": "1001"}]

        async def get_owned(self, user_id: str, order_id: str):
            return {"order_id": order_id}

        async def get_logistics(self, user_id: str, **kwargs):
            return {"order_id": "1001", "trace": []}

    monkeypatch.setattr("app.services.orders.OrderRepository", FakeOrderRepo)
    factory = FakeSessionFactory()
    orders = OrderData(factory)  # type: ignore[arg-type]

    assert await orders.list_orders("user-a") == [{"order_id": "1001"}]
    assert (seed_calls, begins) == (1, 2)  # first access: 1 seed tx + 1 read tx

    # Subsequent reads (even via a new OrderData wrapper over the same factory) open ONLY 1 read tx!
    assert await orders.get_order("user-a", "1001") == {"order_id": "1001"}
    assert await OrderData(factory).get_logistics("user-a", order_id="1001") == {  # type: ignore[arg-type]
        "order_id": "1001",
        "trace": [],
    }
    assert (seed_calls, begins) == (1, 4)


def test_alembic_offline_sql_succeeds_without_llm_keys_and_emits_frozen_ddl() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OPENAI_API_KEY", "CHAT_BASE_URL", "OPENAI_BASE_URL", "OPENAI_MODEL"}
    }
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["DATABASE_URL"] = "mysql+aiomysql://u:p@127.0.0.1:3306/pawhelp"

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    sql = proc.stdout
    assert "CREATE TABLE tool_audit_logs" in sql
    assert "CREATE TABLE review_queue" in sql
    assert "ENUM('pending','done','failed','deleting')" in sql
    assert "CREATE INDEX idx_qa_staging_source_ref ON qa_extraction_staging (source_ref)" in sql
    assert "ADD CONSTRAINT fk_review_published_chunk FOREIGN KEY(published_chunk_id) REFERENCES knowledge_chunks (id) ON DELETE SET NULL" in sql
    assert "DROP TABLE faq" in sql


# ---------------------------------------------------------------------------
# 5. Evaluation Engine Model Reuse, Singleflight Planner & Symmetric k
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_reuses_injected_models_deduplicates_planner_and_aligns_metric_k(
    monkeypatch, tmp_path: Path
) -> None:
    # 1) CachedQueryPlanner singleflight under concurrent strategy calls
    delegate_calls = 0

    class SlowPlanner:
        async def understand(self, query: str) -> QueryPlan:
            nonlocal delegate_calls
            delegate_calls += 1
            await asyncio.sleep(0.02)
            return QueryPlan(normalized_query=query)

    cached = CachedQueryPlanner(SlowPlanner())
    plans = await asyncio.gather(*(cached.understand("同一问题") for _ in range(4)))
    assert delegate_calls == 1
    assert all(p.normalized_query == "同一问题" for p in plans)

    # 2) Symmetric k=5 truncation on both reciprocal_rank and group_retrieval_scores
    assert reciprocal_rank([10, 20, 30, 40, 50, 99], [99], k=5) == 0.0
    assert reciprocal_rank([10, 20, 30, 40, 50, 99], [99]) == pytest.approx(1 / 6)
    assert group_retrieval_scores([10, 20, 30, 40, 50, 99], [(99,)], k=5) == (0.0, 0.0)

    # 3) run_retrieval_evaluation reuses injected embedder/reranker/vector_store/session_factory
    # without instantiating duplicate BGE models or disposing caller's engine.
    monkeypatch.setattr(
        "app.evaluation.runtime.BgeM3Embedder",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must reuse injected embedder")),
    )
    monkeypatch.setattr(
        "app.evaluation.runtime.BgeReranker",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must reuse injected reranker")),
    )
    monkeypatch.setattr(
        "app.evaluation.runtime.create_async_engine_from_settings",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must reuse injected session_factory")),
    )
    monkeypatch.setattr("app.evaluation.runtime.create_chat_model", lambda *a, **kw: SimpleNamespace())

    chunk_rows = [
        SimpleNamespace(
            id=i,
            category="售后",
            questions=f"问题{i}",
            answer="三天内退款" if i == 6 else f"无关{i}",
            section_path=f"章节{i}",
            content_type="policy",
            source_kind="markdown",
            source_path="policies/returns.md",
            vector_id=str(i),
            vectorize_status="done",
        )
        for i in range(1, 8)
    ]

    class InjectedSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalars(self, _stmt):
            return chunk_rows

        async def scalar(self, _stmt):
            return len(chunk_rows)

    cases_file = tmp_path / "mini_cases.jsonl"
    cases_file.write_text(
        EvalCase(
            case_id="T1",
            query="多久退款",
            query_type="A_policy",
            difficulty="easy",
            relevant_chunk_ids=[6],  # placed at rank 6 -> outside Top-5 for ALL strategies!
            expected_points=["三天内退款"],
            reference_answer="三天内退款",
            expected_answerable=True,
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    hits = [(str(i), 1.0 - i * 0.05) for i in range(1, 8)]
    injected_store = SimpleNamespace(
        search_dense=lambda *a, **kw: hits,
        search_bm25=lambda *a, **kw: hits,
        search_hybrid=lambda *a, **kw: hits,
    )
    injected_embedder = SimpleNamespace(embed=lambda texts: [[0.1] * VECTOR_DIMENSION for _ in texts])
    injected_reranker = SimpleNamespace(
        rerank=lambda query, rows, *, limit: [
            SimpleNamespace(chunk=r, score=1.0 - idx * 0.05) for idx, r in enumerate(rows[:limit])
        ]
    )

    report, (json_path, md_path) = await run_retrieval_evaluation(
        _make_settings(),
        cases_file,
        tmp_path / "out",
        generate_answers=False,
        normalize_queries=False,
        embedder=injected_embedder,  # type: ignore[arg-type]
        reranker=injected_reranker,  # type: ignore[arg-type]
        vector_store=injected_store,  # type: ignore[arg-type]
        session_factory=lambda: InjectedSession(),  # type: ignore[arg-type]
    )
    assert json_path.is_file() and md_path.is_file()
    # Rank-6 hit is symmetrically excluded from Top-5 Recall@5 and MRR@5 across all four strategies.
    for strategy_name in ("dense", "bm25", "hybrid", "hybrid_rerank"):
        metrics = report["strategies"][strategy_name]
        assert metrics["recall_at_k"] == 0.0
        assert metrics["mrr"] == 0.0
