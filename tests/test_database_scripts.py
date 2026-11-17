"""Executable database and maintenance scripts without a live networked MySQL server."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.schemas import KnowledgeMiningResult
import scripts.init_db as init_db
import scripts.mine_conversations as mine_conversations
import scripts.show_context_budget as show_context_budget
from tests.fakes import FakeChatModel, fake_settings

ROOT = Path(__file__).resolve().parents[1]


class FakeEngine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


def test_init_db_upgrades_the_database_to_head(monkeypatch: Any) -> None:
    """The established init command delegates schema evolution to Alembic."""

    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        init_db.command,
        "upgrade",
        lambda config, revision: calls.append((config, revision)),
    )

    init_db.main()

    assert len(calls) == 1
    assert calls[0][1] == "head"
    assert calls[0][0].config_file_name.endswith("alembic.ini")


def test_retired_trap_scripts_are_removed() -> None:
    """Legacy FAQ seed and manual-kind eval seed scripts must not reappear."""

    assert not (ROOT / "scripts" / "seed_db.py").exists()
    assert not (ROOT / "scripts" / "seed_retrieval_eval_knowledge.py").exists()


def test_mine_conversations_builder_wires_consistency_checker(monkeypatch: Any) -> None:
    """CLI conversation mining must enforce the same consistency gate as the admin API."""

    settings = fake_settings()
    monkeypatch.setattr(mine_conversations, "create_chat_model", lambda s: FakeChatModel([]))
    monkeypatch.setattr(mine_conversations, "BgeM3Embedder", lambda *args, **kwargs: object())
    monkeypatch.setattr(mine_conversations, "MilvusKnowledgeStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(mine_conversations, "BgeReranker", lambda *args, **kwargs: object())

    service = mine_conversations.build_knowledge_admin_service(
        settings,
        lambda: None,
        batch_size=7,
    )

    assert service._consistency_checker is not None
    assert service._conversation_extractor is not None
    assert service._conversation_batch_size == 7


async def test_mine_conversations_run_delegates_to_service_and_disposes_engine(
    monkeypatch: Any,
) -> None:
    engine = FakeEngine()
    calls: list[tuple[int | None, bool]] = []

    class FakeAdminService:
        def __init__(self, batch_size: int | None) -> None:
            self.batch_size = batch_size

        async def mine_conversations(self, *, vectorize_after: bool) -> KnowledgeMiningResult:
            calls.append((self.batch_size, vectorize_after))
            return KnowledgeMiningResult(
                conversations=3,
                extracted=2,
                kept=1,
                discarded=1,
                vectorized=1 if vectorize_after else 0,
            )

    monkeypatch.setattr(mine_conversations, "get_settings", fake_settings)
    monkeypatch.setattr(
        mine_conversations,
        "create_async_engine_from_settings",
        lambda settings: engine,
    )
    monkeypatch.setattr(
        mine_conversations,
        "create_session_factory",
        lambda received_engine: object(),
    )
    monkeypatch.setattr(
        mine_conversations,
        "build_knowledge_admin_service",
        lambda settings, factory, *, batch_size=None: FakeAdminService(batch_size),
    )

    result = await mine_conversations.run(batch_size=5, vectorize_after=True)

    assert calls == [(5, True)]
    assert (result.conversations, result.extracted, result.kept, result.discarded, result.vectorized) == (
        3,
        2,
        1,
        1,
        1,
    )
    assert engine.dispose_calls == 1


async def test_show_context_budget_disposes_engine_even_on_error(
    monkeypatch: Any,
) -> None:
    engine = FakeEngine()
    settings = fake_settings(model_context_window=65536)
    monkeypatch.setattr(show_context_budget, "get_settings", lambda: settings)
    monkeypatch.setattr(
        show_context_budget,
        "create_async_engine_from_settings",
        lambda received: engine,
    )
    monkeypatch.setattr(
        show_context_budget,
        "create_session_factory",
        lambda received_engine: (lambda: None),
    )
    monkeypatch.setattr(
        show_context_budget,
        "create_chat_model",
        lambda received: FakeChatModel([]),
    )

    budget = await show_context_budget.run()
    assert budget["window"] == 65536
    assert budget["history"] > 0
    assert engine.dispose_calls == 1

    def broken_service(**kwargs: object) -> SimpleNamespace:
        raise RuntimeError("boom")

    monkeypatch.setattr(show_context_budget, "WorkflowChatService", broken_service)
    with pytest.raises(RuntimeError, match="boom"):
        await show_context_budget.run()
    assert engine.dispose_calls == 2
