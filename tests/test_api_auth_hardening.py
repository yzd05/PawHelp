"""Comprehensive regression suite for API authorization, citation sanitization, UTF-8 bounds, and web hardening."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

import app.api.evaluation as evaluation_module
import app.evaluation.runtime as evaluation_runtime
from app.config import Settings
from app.database.models import Conversation, Message, RefundRequest
from app.main import (
    CHAT_PAGE_PATH,
    KNOWLEDGE_PAGE_PATH,
    RAG_EVAL_PAGE_PATH,
    create_app,
)
from app.repositories.orders import OrderRepository
from app.repositories.refunds import RefundRequestConflict, RefundRequestRepository
from app.repositories.tickets import TicketRepository
from app.schemas import (
    KnowledgeChunkView,
    ManualKnowledgeRequest,
    ReviewRequest,
)
from tests.fakes import FakeCombinedModel

DATABASE_URL = "mysql+asyncmy://pawhelp:pawhelp@127.0.0.1:3306/pawhelp"
LOCAL_HEADERS = {"X-PawHelp-Local-Admin": "1"}


def _make_settings(*, admin_token: str = "") -> Settings:
    return Settings(
        chat_base_url="http://localhost:11434/v1",
        openai_model="qwen3",
        openai_api_key="ollama",
        database_url=DATABASE_URL,
        admin_token=SecretStr(admin_token),
    )


def _chunk_row(*, chunk_id: int = 12, status: str = "done") -> SimpleNamespace:
    return SimpleNamespace(
        id=chunk_id,
        category="售后政策",
        questions="七天无理由退货条件是什么？",
        answer="签收次日起 7 天内且商品完好未拆封可申请。",
        section_path="售后政策 / 七天无理由",
        content_type="policy",
        is_key_clause=True,
        vector_id=str(chunk_id),
        vectorize_status=status,
        source_kind="markdown",
        source_path="policies/refund.md",
    )


class _GuardedKnowledgeService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_materials(self):
        self.calls.append("list_materials")
        return []

    def preview(self, path: str):
        self.calls.append("preview")
        return []

    async def overview(self):
        self.calls.append("overview")
        return {
            "materials": 0,
            "mysql_total": 1,
            "pending_total": 0,
            "done_total": 1,
            "milvus_total": 1,
            "milvus_collection": "knowledge_hybrid",
            "dual_write_consistent": True,
        }

    async def preview_markdown(self, request):
        self.calls.append("preview_markdown")
        return {
            "items": [],
            "total": 0,
            "key_clause_count": 0,
            "table_block_count": 0,
            "duplicate_count": 0,
            "ingestible_count": 0,
        }

    async def ingest_markdown(self, request):
        self.calls.append("ingest_markdown")
        return {"inserted": 0, "skipped_duplicates": 0, "skipped_conflicts": 0, "vectorized": 0}

    async def mining_stats(self):
        self.calls.append("mining_stats")
        return {
            "unprocessed_conversations": 0,
            "extracted": 0,
            "kept": 0,
            "discarded": 0,
            "batches": 0,
        }

    async def mine_conversations(self, *, vectorize_after: bool):
        self.calls.append("mine_conversations")
        return {"conversations": 0, "extracted": 0, "kept": 0, "discarded": 0, "vectorized": 0}

    async def list_chunks(self, *, status: str | None, page: int, page_size: int):
        self.calls.append("list_chunks")
        return [_chunk_row()], {"done": 1}

    async def get_chunk(self, chunk_id: int):
        self.calls.append("get_chunk")
        return _chunk_row(chunk_id=chunk_id)

    async def create_manual(self, request):
        self.calls.append("create_manual")
        return _chunk_row(status="pending")

    async def update_chunk(self, chunk_id: int, request):
        self.calls.append("update_chunk")
        return _chunk_row(chunk_id=chunk_id)

    async def delete_chunk(self, chunk_id: int):
        self.calls.append("delete_chunk")

    async def sync_materials(self):
        self.calls.append("sync_materials")
        return {"replaced": 0, "ingested": 0, "vectorized": 0}

    async def vectorize_pending(self):
        self.calls.append("vectorize_pending")
        return 0

    async def search(self, query: str, **kwargs):
        self.calls.append("search")
        return SimpleNamespace(
            matches=(),
            strategy=SimpleNamespace(value="hybrid_rerank"),
            confident=False,
            query_plan=SimpleNamespace(normalized_query=query, synonyms=()),
        )


PROTECTED_ADMIN_ENDPOINTS = [
    ("GET", "/v1/knowledge/materials", None),
    ("GET", "/v1/knowledge/overview", None),
    ("POST", "/v1/knowledge/preview", {"path": "policies/refund.md"}),
    ("POST", "/v1/knowledge/preview-markdown", {"markdown": "# 标题\n\n内容"}),
    ("POST", "/v1/knowledge/ingest-markdown", {"markdown": "# 标题\n\n内容"}),
    ("GET", "/v1/knowledge/mining/stats", None),
    ("POST", "/v1/knowledge/mining/run", {"confirm_external_model_usage": True}),
    ("GET", "/v1/knowledge/chunks", None),
    (
        "POST",
        "/v1/knowledge/chunks",
        {"category": "售后", "questions": "怎么退", "answer": "七天内"},
    ),
    (
        "PUT",
        "/v1/knowledge/chunks/12",
        {"category": "售后", "questions": "怎么退", "answer": "七天内"},
    ),
    ("DELETE", "/v1/knowledge/chunks/12", None),
    ("POST", "/v1/knowledge/sync-materials", {}),
    ("POST", "/v1/knowledge/vectorize-pending", {}),
    ("POST", "/v1/knowledge/search", {"query": "退货"}),
    ("GET", "/v1/rag-eval/report", None),
    ("GET", "/v1/rag-eval/faith-cases", None),
    (
        "POST",
        "/v1/rag-eval/faith-cases/A01/status",
        {
            "status": "无需解决",
            "resolution": "误报",
            "expected_seen_count": 1,
            "expected_status": "未解决",
        },
    ),
    ("POST", "/v1/rag-eval/run", {"confirm_external_model_usage": True}),
    ("POST", "/v1/after-sales/extract", {"description": "买错了想退货"}),
]


async def test_all_admin_and_paid_endpoints_enforce_unified_authorization(monkeypatch) -> None:
    knowledge = _GuardedKnowledgeService()
    eval_calls = 0

    async def fake_eval_runner(full_generation: bool) -> dict[str, object]:
        nonlocal eval_calls
        eval_calls += 1
        return {"strategies": {}, "cases": []}

    monkeypatch.setattr(evaluation_module, "_read_rag_eval_report", lambda: {"cases": []})
    monkeypatch.setattr(
        evaluation_module,
        "report_dataset_alignment",
        lambda payload, path: {"status": "current", "changed_cases": 0},
    )

    settings = _make_settings(admin_token="")
    model = FakeCombinedModel(
        structured_result={
            "order_id": None,
            "request_type": "refund",
            "expected_solution": "退款",
        }
    )
    app = create_app(
        settings=settings,
        model=model,
        knowledge_admin_service=knowledge,  # type: ignore[arg-type]
        rag_eval_runner=fake_eval_runner,
    )

    # 1. Without ADMIN_TOKEN configured, non-local or headerless requests return 503 / 403.
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.1.20", 4321)),
        base_url="http://localhost:8000",
    ) as remote_client:
        for method, path, body in PROTECTED_ADMIN_ENDPOINTS:
            no_header = await remote_client.request(method, path, json=body)
            assert no_header.status_code == 503, f"{method} {path}: {no_header.text}"
            spoofed_local = await remote_client.request(
                method, path, headers=LOCAL_HEADERS, json=body
            )
            assert spoofed_local.status_code == 403, f"{method} {path}: {spoofed_local.text}"

    assert knowledge.calls == []
    assert eval_calls == 0
    assert model.structured_runnable is not None
    assert model.structured_runnable.seen_messages == ()

    # 2. With ADMIN_TOKEN configured, wrong token -> 401; valid token -> allowed even when
    # the console UI simultaneously sends X-PawHelp-Local-Admin: 1 from a LAN browser.
    settings.admin_token = SecretStr("lan-operator-secret")
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.1.20", 4321)),
        base_url="http://192.168.1.50:8000",
    ) as remote_client:
        wrong = await remote_client.post(
            "/v1/rag-eval/run",
            headers={**LOCAL_HEADERS, "X-Admin-Token": "wrong"},
            json={"confirm_external_model_usage": True},
        )
        assert wrong.status_code == 401
        valid = await remote_client.post(
            "/v1/rag-eval/run",
            headers={**LOCAL_HEADERS, "X-Admin-Token": "lan-operator-secret"},
            json={"confirm_external_model_usage": True},
        )
        assert valid.status_code == 200
        assert eval_calls == 1


async def test_citation_chunk_endpoint_sanitizes_public_view_and_preserves_admin_view() -> None:
    knowledge = _GuardedKnowledgeService()
    settings = _make_settings(admin_token="operator-token")
    app = create_app(
        settings=settings,
        model=FakeCombinedModel(),
        knowledge_admin_service=knowledge,  # type: ignore[arg-type]
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.1.30", 5000)),
        base_url="http://192.168.1.50:8000",
    ) as customer_client:
        public_resp = await customer_client.get("/v1/knowledge/chunks/12")
        assert public_resp.status_code == 200
        assert public_resp.json() == {
            "id": 12,
            "category": "售后政策",
            "section_path": "售后政策 / 七天无理由",
            "questions": "七天无理由退货条件是什么？",
            "answer": "签收次日起 7 天内且商品完好未拆封可申请。",
        }
        for internal_field in (
            "vector_id",
            "vectorize_status",
            "source_kind",
            "source_path",
            "content_type",
            "is_key_clause",
        ):
            assert internal_field not in public_resp.json()

        bad_token = await customer_client.get(
            "/v1/knowledge/chunks/12", headers={"X-Admin-Token": "wrong"}
        )
        assert bad_token.status_code == 401

        token_admin = await customer_client.get(
            "/v1/knowledge/chunks/12", headers={"X-Admin-Token": "operator-token"}
        )
        assert token_admin.status_code == 200
        assert token_admin.json()["vector_id"] == "12"
        assert token_admin.json()["vectorize_status"] == "done"
        assert token_admin.json()["source_kind"] == "markdown"
        assert token_admin.json()["source_path"] == "policies/refund.md"

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 5000)),
        base_url="http://localhost:8000",
    ) as local_admin_client:
        local_admin = await local_admin_client.get(
            "/v1/knowledge/chunks/12", headers=LOCAL_HEADERS
        )
        assert local_admin.status_code == 200
        assert local_admin.json()["source_path"] == "policies/refund.md"


def test_schema_utf8_byte_validators_reject_poison_pill_payloads() -> None:
    # 251 4-byte emojis = 251 chars (<= 255 char limit) but 1004 UTF-8 bytes (> 1000B limit).
    with pytest.raises(ValidationError, match="1000 UTF-8 bytes"):
        ManualKnowledgeRequest(
            category="🐾" * 251,
            questions="正常问题",
            answer="正常回答",
        )

    # 501 4-byte emojis = 501 chars (<= 512 char limit) but 2004 UTF-8 bytes (> 2000B limit).
    with pytest.raises(ValidationError, match="2000 UTF-8 bytes"):
        ManualKnowledgeRequest(
            category="售后政策",
            section_path="🐾" * 501,
            questions="正常问题",
            answer="正常回答",
        )

    # Combined questions + answer > 60,000 UTF-8 bytes rejected.
    with pytest.raises(ValidationError, match="60000 UTF-8 bytes"):
        ManualKnowledgeRequest(
            category="售后政策",
            questions="问" * 10_000,
            answer="答" * 10_001,
        )

    valid_manual = ManualKnowledgeRequest(
        category="售后政策",
        section_path="售后政策 / 退换货",
        questions="问" * 5_000,
        answer="答" * 5_000,
    )
    assert valid_manual.category == "售后政策"

    # 7501 4-byte emojis = 7501 chars (<= 10000 char limit) but 30,004 UTF-8 bytes (> 30000B limit).
    with pytest.raises(ValidationError, match="30000 UTF-8 bytes"):
        ReviewRequest(approved_answer="🐾" * 7501)

    assert ReviewRequest(approved_answer="核准答案").approved_answer == "核准答案"

    # VectorizeStatus supports failed / deleting tombstone states for rag-data dual-write hardening.
    failed_view = KnowledgeChunkView.model_validate(_chunk_row(status="failed"))
    deleting_view = KnowledgeChunkView.model_validate(_chunk_row(status="deleting"))
    assert failed_view.vectorize_status == "failed"
    assert deleting_view.vectorize_status == "deleting"


async def test_client_storage_migration_allows_non_loopback_when_no_migration_exists(
    tmp_path: Path,
) -> None:
    app = create_app(
        settings=_make_settings(),
        model=FakeCombinedModel(),
        runtime_root=tmp_path,
    )

    # Non-loopback LAN / Docker access without a migration file must NOT return 403.
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("172.18.0.1", 54321)),
        base_url="http://192.168.1.88:8000",
    ) as lan_client:
        response = await lan_client.get("/v1/client-storage-migration")
        assert response.status_code == 200
        assert response.json() == {"needed": False, "id": None, "rules": []}

    # When a local migration policy file IS present on disk, loopback receives it while remote peers get 403.
    runtime_dir = tmp_path / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "client-storage-migration.json").write_text(
        json.dumps(
            {
                "id": "v1-local",
                "rules": [
                    {
                        "source": "legacy.userId",
                        "target": "pawhelp.userId",
                        "kind": "uuid",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("172.18.0.1", 54321)),
        base_url="http://192.168.1.88:8000",
    ) as lan_client:
        assert (await lan_client.get("/v1/client-storage-migration")).status_code == 403

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 54321)),
        base_url="http://localhost:8000",
    ) as local_client:
        local_resp = await local_client.get("/v1/client-storage-migration")
        assert local_resp.status_code == 200
        assert local_resp.json()["needed"] is True
        assert local_resp.json()["id"] == "v1-local"


def test_default_rag_eval_runner_reuses_app_resources_adaptively(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def modern_eval(
        settings,
        cases_path,
        output_dir,
        *,
        generation_strategies=None,
        embedder=None,
        reranker=None,
        vector_store=None,
        session_factory=None,
    ):
        captured.update(
            embedder=embedder,
            reranker=reranker,
            vector_store=vector_store,
            session_factory=session_factory,
        )
        return {"strategies": {"hybrid_rerank": {"mrr": 0.95}}, "cases": []}, (
            Path("a.json"),
            Path("b.md"),
        )

    monkeypatch.setattr(evaluation_runtime, "run_retrieval_evaluation", modern_eval)
    app = create_app(settings=_make_settings(), model=FakeCombinedModel())

    with TestClient(
        app,
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
        headers=LOCAL_HEADERS,
    ) as client:
        res = client.post(
            "/v1/rag-eval/run",
            json={"confirm_external_model_usage": True, "full_generation": False},
        )

    assert res.status_code == 200
    assert captured["embedder"] is app.state.embedder
    assert captured["reranker"] is app.state.reranker
    assert captured["vector_store"] is app.state.vector_store
    assert captured["session_factory"] is app.state.session_factory


async def test_ticket_and_refund_actions_persist_idempotent_confirmations_and_409_messages(
    monkeypatch,
) -> None:
    owner_id = str(uuid4())
    conversation = Conversation(id=7, user_id=owner_id, status="进行中")
    stored_messages: list[Message] = [
        Message(
            id=1,
            conversation_id=7,
            role="user",
            content="订单 1001 想申请售后",
        ),
        Message(
            id=2,
            conversation_id=7,
            role="assistant",
            content="您可以点击下方按钮提交工单或申请退款。",
            suggested_actions=["建工单", "申请退款"],
            related_order_id="1001",
        ),
        Message(
            id=3,
            conversation_id=7,
            role="assistant",
            content="待付款订单说明。",
            suggested_actions=["申请退款"],
            related_order_id="1003",
        ),
    ]
    refund_store: dict[str, RefundRequest] = {}
    ticket_store: dict[str, SimpleNamespace] = {}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @asynccontextmanager
        async def begin(self):
            yield self

        async def get(self, model, identity):
            if model is Conversation and identity == 7:
                return conversation
            return None

        async def scalar(self, statement):
            sql = str(statement)
            if "messages.suggested_actions" in sql or "messages.role" in sql:
                params = statement.compile().params
                for val in params.values():
                    if isinstance(val, str) and (
                        val.startswith("工单已创建：") or val.startswith("退款申请已提交：")
                    ):
                        prefix = val.removesuffix("%")
                        for msg in stored_messages:
                            if msg.role == "assistant" and (msg.content or "").startswith(prefix):
                                return msg.id
                        return None
                target_id = next(
                    (v for k, v in params.items() if "id" in k and v != 7), None
                )
                return next((m for m in stored_messages if m.id == target_id), None)
            return None

        async def scalars(self, statement):
            return list(stored_messages)

        def add(self, obj):
            if isinstance(obj, Message):
                obj.id = len(stored_messages) + 1
                stored_messages.append(obj)

        async def flush(self):
            return None

    async def fake_ticket_create_or_get(
        self, *, ticket_no, conversation_id, description, ticket_type, status
    ):
        created = ticket_no not in ticket_store
        if created:
            ticket_store[ticket_no] = SimpleNamespace(
                ticket_no=ticket_no,
                status=status,
                created_at=datetime(2026, 10, 1, 8, 0, 0),
            )
        return ticket_store[ticket_no], created

    async def fake_get_owned_order(self, user_id: str, order_id: str):
        if order_id == "1003":
            return {"order_id": "1003", "status": "pending_payment"}
        return {"order_id": order_id, "status": "delivered"}

    async def fake_refund_create_or_get(
        self,
        *,
        conversation_id,
        source_message_id,
        order_id,
        order_user_id,
        reason,
        description,
    ):
        request_no = f"REFUND-{source_message_id}"
        existing = refund_store.get(request_no)
        if existing is not None:
            if existing.reason != reason or existing.description != description:
                raise RefundRequestConflict("conflict")
            return existing
        row = RefundRequest(
            request_no=request_no,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            order_id=order_id,
            order_user_id=order_user_id,
            reason=reason,
            description=description,
            status="待人工审核",
            created_at=datetime(2026, 10, 1, 8, 5, 0),
        )
        refund_store[request_no] = row
        return row

    monkeypatch.setattr(TicketRepository, "create_or_get", fake_ticket_create_or_get)
    monkeypatch.setattr(OrderRepository, "get_owned", fake_get_owned_order)
    monkeypatch.setattr(RefundRequestRepository, "create_or_get", fake_refund_create_or_get)

    app = create_app(
        settings=_make_settings(),
        model=FakeCombinedModel(),
        session_factory=_FakeSession,  # type: ignore[arg-type]
        tool_chat_service=object(),  # type: ignore[arg-type]
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ticket_payload = {
            "user_id": owner_id,
            "message_id": 2,
            "ticket_type": "售后",
            "description": "包装破损需要跟进",
        }
        t1 = await client.post("/v1/conversations/7/tickets", json=ticket_payload)
        t2 = await client.post("/v1/conversations/7/tickets", json=ticket_payload)
        assert t1.status_code == t2.status_code == 200
        assert t1.json() == t2.json()

        unpaid = await client.post(
            "/v1/conversations/7/refund-requests",
            json={
                "user_id": owner_id,
                "message_id": 3,
                "order_id": "1003",
                "reason": "七天无理由",
            },
        )
        assert unpaid.status_code == 409
        assert unpaid.json()["detail"]["message"] == "订单尚未付款，无需申请退款"

        refund_payload = {
            "user_id": owner_id,
            "message_id": 2,
            "order_id": "1001",
            "reason": "商品破损",
            "description": "外箱压坏",
        }
        r1 = await client.post("/v1/conversations/7/refund-requests", json=refund_payload)
        r2 = await client.post("/v1/conversations/7/refund-requests", json=refund_payload)
        conflict = await client.post(
            "/v1/conversations/7/refund-requests",
            json={**refund_payload, "reason": "质量问题"},
        )
        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "refund_request_already_submitted"
        assert conflict.json()["detail"]["message"]

        history = await client.get(
            "/v1/conversations/7/messages", headers={"X-Demo-User-Id": owner_id}
        )
        assert history.status_code == 200
        contents = [row["content"] for row in history.json()]
        assert sum(text.startswith("工单已创建：") for text in contents) == 1
        assert sum(text.startswith("退款申请已提交：") for text in contents) == 1


def test_all_three_pages_have_no_inline_scripts_or_styles() -> None:
    for path in (CHAT_PAGE_PATH, KNOWLEDGE_PAGE_PATH, RAG_EVAL_PAGE_PATH):
        html = path.read_text(encoding="utf-8")
        assert "<style" not in html, f"{path.name} still has inline <style>"
        for line in html.splitlines():
            if "<script" in line:
                assert "src=" in line, f"{path.name} still has inline <script>: {line}"
