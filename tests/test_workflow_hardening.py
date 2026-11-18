"""Regression tests for workflow, memory, prompt, MCP catalog, and observability hardening."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from dataclasses import fields
from pathlib import Path
from random import Random
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    select,
    text,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.context import ContextBudgetExceeded
from app.database.models import (
    Conversation,
    ConversationSummary,
    KnowledgeChunk,
    RequestObservation,
)
from app.database.session import (
    MYSQL_UTC_INIT_COMMAND,
    create_async_engine_from_settings,
)
from app.knowledge.chunking import KnowledgeDraft
from app.knowledge.confidence import EvidenceGate, scoring_fingerprint
from app.knowledge.evidence import EvidenceDecision
from app.knowledge.jev_evidence import (
    INSTRUCTIONS_SHA256,
    JevEvidenceJudge,
    JevRoutedChecker,
)
from app.knowledge.query_understanding import QueryPlan
from app.knowledge.search import KnowledgeMatch, RetrievalResult, RetrievalStrategy
from app.llm import create_chat_model, create_fast_chat_model
from app.observability import Observability, is_allowed_self_hosted_langfuse_url
from app.prompts import (
    CONVERSATION_KNOWLEDGE_EXTRACTION_SYSTEM_TEMPLATE,
    CUSTOMER_SERVICE_SYSTEM_TEMPLATE,
    render_conversation_knowledge_messages,
    render_customer_service_messages,
    render_workflow_system_prompt,
)
from app.repositories.conversations import ConversationRepository
from app.repositories.knowledge import KnowledgeRepository
from app.repositories.messages import MessageRepository
from app.schemas import ChatRequest
from app.services.tool_chat import ChatService, PreparedTurn
from app.tools.business import ToolRuntime, build_business_tools
from app.tools.catalog import MultiServerMCPClient, ToolCatalog
from app.tools.executor import ToolExecutor
from app.workflow.context_logging import configure_context_logging
from app.workflow.graph import WorkflowChatService
from app.workflow.memory import (
    SUMMARY_PROMPT,
    ConversationMemory,
    _allowed_summary_numbers,
    resolve_model_context_window,
)
from app.workflow.titles import ConversationTitles, render_title_prompt
from app.workflow.understanding import classify_intent
from mcp_servers.scenario import get_demo_settings
from tests.fakes import fake_settings
from tests.order_fakes import FakeOrderData, TestToolCatalog
from tests.workflow_fakes import (
    InMemoryWorkflow,
    LabelledModel,
    LabelledSearch,
    tool_call,
    turn,
)

# ---------------------------------------------------------------------------
# 1. [P0] Default mainstream model context windows & validate_budget()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name",
    [
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-v3-0324",
        "deepseek-ai/DeepSeek-V3",
        "deepseek-v4-flash",
        "gpt-4o",
        "gpt-4.1-mini",
        "gpt-5-mini",
        "qwen-plus",
        "Qwen/Qwen3-32B",
        "claude-3-7-sonnet-20250219",
        "gemini-2.5-flash",
        "glm-4-plus",
    ],
)
def test_mainstream_models_have_out_of_box_context_window(model_name: str) -> None:
    window = resolve_model_context_window(model_name)
    assert window is not None and window >= 65536

    cfg = fake_settings(openai_model=model_name, model_context_window=0)
    memory = ConversationMemory(cfg, None, None, "系统规则", "[]")
    assert memory.window_known is True
    assert memory.budget.history > 0
    memory.validate_budget()


def test_unknown_or_undersized_context_window_fails_validate_budget_cleanly() -> None:
    unknown_memory = ConversationMemory(
        fake_settings(openai_model="unknown-custom-model", model_context_window=0),
        None,
        None,
        "系统规则",
        "[]",
    )
    assert unknown_memory.window_known is False
    with pytest.raises(ContextBudgetExceeded, match="MODEL_CONTEXT_WINDOW"):
        unknown_memory.validate_budget()

    tiny_memory = ConversationMemory(
        fake_settings(openai_model="deepseek-chat", model_context_window=4096),
        None,
        None,
        "系统规则",
        "[]",
    )
    assert tiny_memory.budget.history == 0
    with pytest.raises(ContextBudgetExceeded, match="MODEL_CONTEXT_WINDOW"):
        tiny_memory.validate_budget()


@pytest.mark.asyncio
async def test_default_deepseek_chat_turn_succeeds_without_explicit_window(context_sessions) -> None:
    cfg = fake_settings(openai_model="deepseek-chat", model_context_window=0, mcp_enabled=False)
    model = LabelledModel("闲聊")
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    try:
        turn = await service.prepare_turn(ChatRequest(user_id=uuid4(), message="你好"))
        events = [event async for event in service.stream_turn(turn)]
        assert events[-1].name == "done"
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# 2. [P1] Standalone WorkflowChatService, done telemetry & checkpoint rebuild
# ---------------------------------------------------------------------------


def test_workflow_service_has_no_dead_single_step_inheritance() -> None:
    assert WorkflowChatService.__bases__ == (object,)
    assert not hasattr(WorkflowChatService, "_stream_started_turn")
    assert not hasattr(WorkflowChatService, "_reject_knowledge")
    assert not hasattr(WorkflowChatService, "_persist_tool")


@pytest.mark.asyncio
async def test_checkpoint_rebuild_from_db_restores_queried_orders_and_telemetry(context_sessions) -> None:
    cfg = fake_settings(
        openai_model="deepseek-chat",
        model_context_window=26000,
        max_user_input_tokens=600,
        model_max_output_tokens=500,
        tool_result_max_tokens=1200,
        mcp_enabled=False,
    )

    class InspectingModel(LabelledModel):
        def __init__(self) -> None:
            super().__init__(
                "订单",
                [
                    tool_call("query_order", '{"order_id":"1001"}', "call-order-1001"),
                    "订单1001猫爬架已签收。",
                    "已根据历史查单记录核实订单1001。",
                ],
            )
            self.seen_understanding: list[list] = []

        def with_structured_output(self, schema, *, method):
            parent = self
            base = super().with_structured_output(schema, method=method)

            class Wrapper:
                async def ainvoke(self, messages):
                    if schema.__name__ == "ResolvedQuestion":
                        parent.seen_understanding.append(list(messages))
                    return await base.ainvoke(messages)

            return Wrapper()

    model = InspectingModel()
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    user = str(uuid4())
    try:
        t1 = await service.prepare_turn(ChatRequest(user_id=user, message="查询订单1001"))
        e1 = [e async for e in service.stream_turn(t1)]
        assert e1[-1].name == "done"
        assert e1[-1].data["input_tokens_estimate"] > 100

        # Wipe in-memory checkpoints to simulate deleting conversation-checkpoints.sqlite3.
        service.set_checkpointer(InMemorySaver())

        t2 = await service.prepare_turn(
            ChatRequest(user_id=user, conversation_id=t1.conversation_id, message="它现在什么状态")
        )
        e2 = [e async for e in service.stream_turn(t2)]
        assert e2[-1].name == "done"
        assert model.seen_understanding, "turn 2 with pronoun and history must run resolver"
        material_msg = model.seen_understanding[-1][1].content
        assert '"order_id": "1001"' in material_msg
        assert '"product": "猫爬架"' in material_msg

        # Verify checkpoint state does NOT store duplicate transient message copies.
        snap = await service._checkpointer.aget_tuple({"configurable": {"thread_id": f"{user}:{t1.conversation_id}"}})
        channel_values = snap.checkpoint["channel_values"]
        assert "messages" in channel_values
        for transient_key in ("history_messages", "understanding_messages", "agent_context", "history_summary"):
            assert transient_key not in channel_values
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_done_event_reports_trimmed_messages_when_history_exceeds_budget(context_sessions) -> None:
    cfg = fake_settings(
        model_context_window=24000,
        max_user_input_tokens=600,
        model_max_output_tokens=500,
        tool_result_max_tokens=1200,
        mcp_enabled=False,
    )
    model = LabelledModel("闲聊")
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    user = str(uuid4())
    try:
        async with context_sessions() as session, session.begin():
            conv = await ConversationRepository(session).create(user)
            repo = MessageRepository(session)
            for idx in range(25):
                await repo.add_user(conv.id, f"第{idx}轮长文本" + "历史对话内容" * 60)
                await repo.add_assistant(conv.id, "历史助手回复" * 60)

        turn = await service.prepare_turn(
            ChatRequest(user_id=user, conversation_id=conv.id, message="你好")
        )
        events = [e async for e in service.stream_turn(turn)]
        done = events[-1]
        assert done.name == "done"
        assert done.data["trimmed_messages"] > 0
    finally:
        await service.close()


# ---------------------------------------------------------------------------
# 3. [P1] Fast model client, first-turn skip, live token streaming & MCP cache
# ---------------------------------------------------------------------------


def test_create_fast_chat_model_disables_streaming_and_uses_fast_model_override() -> None:
    default_fast = create_fast_chat_model(fake_settings(openai_model="deepseek-chat"))
    assert default_fast.streaming is False
    assert default_fast.stream_usage is False
    assert default_fast.model_name == "deepseek-chat"
    assert default_fast.request_timeout == 15.0
    assert default_fast.max_retries == 1

    overridden = create_fast_chat_model(
        fake_settings(
            openai_model="deepseek-reasoner",
            fast_openai_model="deepseek-chat",
            fast_model_timeout_seconds=8.5,
        )
    )
    assert overridden.streaming is False
    assert overridden.model_name == "deepseek-chat"
    assert overridden.request_timeout == 8.5
    assert overridden.max_retries == 1

    streaming_main = create_chat_model(fake_settings(openai_model="deepseek-chat"))
    assert streaming_main.streaming is True
    assert streaming_main.stream_usage is True
    assert streaming_main.request_timeout == 60
    assert streaming_main.max_retries == 2


@pytest.mark.asyncio
async def test_first_turn_clear_question_skips_resolve_question_llm_call(context_sessions) -> None:
    cfg = fake_settings(openai_model="deepseek-chat", mcp_enabled=False)
    model = LabelledModel("订单", ["请提供您需要查询的具体事项。"])
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    user = str(uuid4())
    try:
        first = await service.prepare_turn(ChatRequest(user_id=user, message="查询订单1001的状态"))
        events = [e async for e in service.stream_turn(first)]
        assert events[-1].name == "done"
        assert model.resolution_calls == 0

        # Turn 2 has conversation history, so resolver runs.
        model.outputs.append("订单1001已签收。")
        second = await service.prepare_turn(
            ChatRequest(user_id=user, conversation_id=first.conversation_id, message="它发货了吗")
        )
        events2 = [e async for e in service.stream_turn(second)]
        assert events2[-1].name == "done"
        assert model.resolution_calls == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_business_route_streams_tokens_incrementally_and_strips_action_tag(context_sessions) -> None:
    gate = asyncio.Event()

    class MultiChunkBusinessModel(LabelledModel):
        async def astream(self, messages):
            self.agent_calls += 1
            yield AIMessageChunk(content="订单1001")
            await gate.wait()
            yield AIMessageChunk(content="目前正在派送中")
            yield AIMessageChunk(content="。\n[建议")
            yield AIMessageChunk(content="选项:转人工,建工单]  ")

    cfg = fake_settings(openai_model="deepseek-chat", mcp_enabled=False)
    model = MultiChunkBusinessModel("订单")
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    try:
        turn = await service.prepare_turn(ChatRequest(user_id=uuid4(), message="查订单1001进展"))
        stream = service.stream_turn(turn)
        ev_conv = await anext(stream)
        assert ev_conv.name == "conversation"
        # First token arrives BEFORE gate is set — proving true incremental TTFT!
        first_token = await asyncio.wait_for(anext(stream), timeout=2)
        assert first_token.name == "token"
        assert first_token.data["content"] == "订单1001"

        gate.set()
        rest = [event async for event in stream]
        tokens = [first_token.data["content"], *(e.data["content"] for e in rest if e.name == "token")]
        assert len(tokens) >= 2
        assert "".join(tokens) == "订单1001目前正在派送中。"
        assert all("建议选项" not in part for part in tokens)

        done = rest[-1]
        assert done.name == "done"
        assert done.data["content"] == "订单1001目前正在派送中。"
        assert done.data["suggested_actions"] == ["转人工", "建工单"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_business_route_char_by_char_streaming_strips_two_option_action_tag(context_sessions) -> None:
    """Character-by-character streaming must never leak partial two-option tags (e.g. '[建议选项:转人工,建')."""

    class CharByCharModel(LabelledModel):
        async def astream(self, messages):
            self.agent_calls += 1
            for ch in "订单1001目前正在派送中。\n[建议选项:转人工,建工单]  ":
                yield AIMessageChunk(content=ch)

    cfg = fake_settings(openai_model="deepseek-chat", mcp_enabled=False)
    char_service = WorkflowChatService(
        settings=cfg,
        model=CharByCharModel("订单"),
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    try:
        char_turn = await char_service.prepare_turn(ChatRequest(user_id=uuid4(), message="逐字流式测试订单1001"))
        char_events = [e async for e in char_service.stream_turn(char_turn)]
        char_tokens = [e.data["content"] for e in char_events if e.name == "token"]
        assert len(char_tokens) >= 10
        assert "".join(char_tokens) == "订单1001目前正在派送中。"
        assert all("[" not in part and "建议选项" not in part for part in char_tokens)
        assert char_events[-1].name == "done"
        assert char_events[-1].data["suggested_actions"] == ["转人工", "建工单"]
    finally:
        await char_service.close()


@pytest.mark.asyncio
async def test_resume_order_with_non_layered_context_does_not_duplicate_prefetch_messages() -> None:
    from langchain_core.messages import ToolMessage

    from tests.workflow_fakes import InMemoryWorkflow, ScriptedUnderstandingModel, turn

    model = ScriptedUnderstandingModel(
        [{"intent": "物流", "confidence": 0.95, "task_kind": "progress_query"}],
        outputs=["订单1001已签收。"],
    )
    service = InMemoryWorkflow(model, LabelledSearch(True))
    initial = [e async for e in service.stream_turn(turn("查询订单物流", history=[HumanMessage(content="你好")]))]
    assert initial[-1].name == "order_selection"

    resumed = [e async for e in service.resume_order("demo", 1, "1001", 1)]
    assert resumed[-1].name == "done"
    seen_ctx = model.agent_histories[-1]
    tool_msgs = [m for m in seen_ctx if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1, f"expected exactly 1 prefetched ToolMessage, got {len(tool_msgs)}"


@pytest.mark.asyncio
async def test_lazy_tool_snapshot_and_mcp_ttl_backoff_and_policy_cache(tmp_path, monkeypatch, context_sessions) -> None:
    class CountingCatalog(TestToolCatalog):
        def __init__(self) -> None:
            self.calls = 0

        async def snapshot(self, runtime):
            self.calls += 1
            return await super().snapshot(runtime)

    counting = CountingCatalog()
    cfg = fake_settings(openai_model="deepseek-chat", mcp_enabled=False)
    model = LabelledModel("闲聊", ["此前未查过订单。"])
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=counting,
        knowledge_search=LabelledSearch(True),
    )
    user = str(uuid4())
    try:
        # Smalltalk and recall never invoke catalog.snapshot().
        for msg in ("你好", "我之前查询的是哪个订单"):
            t = await service.prepare_turn(ChatRequest(user_id=user, message=msg))
            _ = [e async for e in service.stream_turn(t)]
        assert counting.calls == 0
    finally:
        await service.close()

    # Verify ToolCatalog TTL cache, runtime user isolation, policy mtime hot reload, and failure backoff.
    seen_users: list[str] = []
    discover_calls = {"logistics": 0, "aftersales": 0}
    fail_aftersales = True

    @tool
    async def mcp_probe() -> str:
        """Probe runtime isolation."""
        from app.tools.catalog import _active_runtime

        rt = _active_runtime.get()
        seen_users.append(rt.user_id if rt else "missing")
        return '{"status":"ok"}'

    async def fake_get_tools(self, *, server_name=None):
        discover_calls[server_name] += 1
        if server_name == "aftersales" and fail_aftersales:
            raise ConnectionError("aftersales down")
        return [mcp_probe] if server_name == "logistics" else []

    monkeypatch.setattr(MultiServerMCPClient, "get_tools", fake_get_tools)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"mcp:logistics:mcp_probe": "deny"}), encoding="utf-8")

    cat_cfg = fake_settings(
        mcp_enabled=True,
        tool_policy_path=str(policy_path),
        tool_plugins_path=str(tmp_path / "no_plugins"),
    )
    catalog = ToolCatalog(cat_cfg, discovery_ttl_seconds=30.0, failure_backoff_seconds=20.0)

    rt_a = ToolRuntime(1, lambda: None, Random(), user_id="user-A", order_data=FakeOrderData())
    rt_b = ToolRuntime(2, lambda: None, Random(), user_id="user-B", order_data=FakeOrderData())

    reg1 = await catalog.snapshot(rt_a)
    assert reg1.spec("mcp_probe").permission == "deny"
    assert discover_calls == {"logistics": 1, "aftersales": 1}

    # Update policy file: even while MCP discovery is TTL-cached and aftersales is in backoff,
    # policy permission updates immediately AND no second network discovery occurs!
    policy_path.write_text(
        json.dumps({"mcp:logistics:mcp_probe": "read", "unused_padding": True}),
        encoding="utf-8",
    )
    reg_a = await catalog.snapshot(rt_a)
    reg_b = await catalog.snapshot(rt_b)
    assert discover_calls == {"logistics": 1, "aftersales": 1}
    assert reg_a.spec("mcp_probe").permission == "read"

    res_a = await ToolExecutor(reg_a).execute({"id": "c-a", "name": "mcp_probe", "args": {}})
    res_b = await ToolExecutor(reg_b).execute({"id": "c-b", "name": "mcp_probe", "args": {}})
    assert res_a.ok and res_b.ok
    assert seen_users == ["user-A", "user-B"]


def test_demo_settings_is_lru_cached() -> None:
    get_demo_settings.cache_clear()
    first = get_demo_settings()
    second = get_demo_settings()
    assert first is second


# ---------------------------------------------------------------------------
# 4. [P1] Summary guards, safe prompts, non-leaking locks & async observability
# ---------------------------------------------------------------------------


def test_summary_prompt_limit_and_chinese_numeral_allowance() -> None:
    assert "不超过120个字" in SUMMARY_PROMPT
    allowed = _allowed_summary_numbers("顾客买了两千块的猫爬架，另一单是一万二，口述订单一零零一，还有订单1002。")
    assert {"2000", "12000", "1001", "1002"} <= allowed
    assert "9999" not in allowed


@pytest.mark.asyncio
async def test_summary_failure_cap_stops_repeated_paid_retries_and_preserves_anchor(context_sessions) -> None:
    summary_invocations = 0

    class AlwaysFailingSummaryModel:
        def with_structured_output(self, schema, **kwargs):
            class Runnable:
                async def ainvoke(self, messages):
                    nonlocal summary_invocations
                    summary_invocations += 1
                    # Invent an identifier 999999 not in source to trigger validation failure.
                    return {"content": "用户询问订单999999。"}

            return Runnable()

    cfg = fake_settings(
        model_context_window=24000,
        max_user_input_tokens=600,
        model_max_output_tokens=500,
        tool_result_max_tokens=1200,
    )
    memory = ConversationMemory(
        cfg,
        context_sessions,
        AlwaysFailingSummaryModel(),
        "系统" * 4000,
        "[]",
        max_summary_attempts=2,
    )
    user = str(uuid4())
    async with context_sessions() as session, session.begin():
        conv = await ConversationRepository(session).create(user)
        repo = MessageRepository(session)
        rows = []
        for idx in range(22):
            rows.append(await repo.add_user(conv.id, f"订单1001第{idx}轮" + "业务说明" * 50))
            rows.append(await repo.add_assistant(conv.id, "补充说明" * 50))
        current = await repo.add_user(conv.id, "后续问题")

    from app.services.history import to_langchain_messages

    view = await memory.prepare(conv.id, user, to_langchain_messages([*rows, current]), current.id)
    for _ in range(5):
        memory.schedule(conv.id, user, view)
        if memory.tasks:
            await asyncio.gather(*list(memory.tasks.values()))

    assert summary_invocations == 2
    async with context_sessions() as session:
        refreshed = await session.get(Conversation, conv.id)
        segments = list(
            await session.scalars(select(ConversationSummary).where(ConversationSummary.conversation_id == conv.id))
        )
    assert refreshed.summary_upto_msg_id is None
    assert segments == []
    await memory.close()


def test_prompts_reject_query_faq_and_survive_braces_in_shop_name() -> None:
    assert "query_faq" not in CUSTOMER_SERVICE_SYSTEM_TEMPLATE
    tricky_shop = "萌宠{旗舰}店{raw_key}"
    system_prompt = render_workflow_system_prompt(tricky_shop)
    assert tricky_shop in system_prompt
    assert "query_faq" not in system_prompt

    rendered_msgs = render_customer_service_messages(tricky_shop, [HumanMessage(content="你好")])
    assert tricky_shop in str(rendered_msgs[0].content)

    title_prompt = render_title_prompt(tricky_shop)
    assert tricky_shop in title_prompt


def test_context_logging_disables_propagation_and_hides_raw_payloads_at_info(tmp_path, caplog) -> None:
    detach = configure_context_logging(tmp_path)
    wf_logger = logging.getLogger("app.workflow")
    try:
        assert wf_logger.propagate is False
        memory = ConversationMemory(fake_settings(openai_model="deepseek-chat"), None, None, "sys", '[{"name":"secret_schema"}]')
        with caplog.at_level(logging.INFO, logger="app.workflow.memory"):
            memory.log_context("model_ctx", 1, [HumanMessage(content="敏感手机号13800138000")])
    finally:
        detach()

    log_text = (tmp_path / "log" / "app.log").read_text(encoding="utf-8")
    assert "13800138000" not in log_text
    assert "secret_schema" not in log_text


@pytest.mark.asyncio
async def test_prepare_turn_without_stream_does_not_deadlock_or_share_new_lock(context_sessions) -> None:
    cfg = fake_settings(openai_model="deepseek-chat", mcp_enabled=False)
    model = LabelledModel("闲聊")
    service = WorkflowChatService(
        settings=cfg,
        model=model,
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    user = str(uuid4())
    try:
        # Call prepare_turn and abandon it without ever calling stream_turn!
        abandoned = await service.prepare_turn(ChatRequest(user_id=user, message="未消费的请求"))
        assert not service._lock_for(user, abandoned.conversation_id).locked()

        # Subsequent turn on the same conversation and concurrent new conversations succeed immediately.
        follow_up = await asyncio.wait_for(
            service.prepare_turn(ChatRequest(user_id=user, conversation_id=abandoned.conversation_id, message="你好")),
            timeout=1.0,
        )
        concurrent_new = await asyncio.wait_for(
            asyncio.gather(
                service.prepare_turn(ChatRequest(user_id=user, message="新会话A")),
                service.prepare_turn(ChatRequest(user_id=user, message="新会话B")),
            ),
            timeout=1.0,
        )
        assert concurrent_new[0].conversation_id != concurrent_new[1].conversation_id
        events = [e async for e in service.stream_turn(follow_up)]
        assert events[-1].name == "done"
    finally:
        await service.close()


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("http://127.0.0.1:3000", True),
        ("http://localhost:3000", True),
        ("http://langfuse-web:3000", True),
        ("http://host.docker.internal:3000", True),
        ("http://langfuse.svc.cluster.local:3000", True),
        ("http://172.18.0.5:3000", True),
        ("https://cloud.langfuse.com", False),
        ("https://us.cloud.langfuse.com", False),
        ("https://8.8.8.8:3000", False),
        ("http://user:pass@localhost:3000", False),
        ("https://public.example.com", False),
    ],
)
def test_langfuse_url_validation_allows_container_hosts_and_blocks_cloud(url: str, allowed: bool) -> None:
    assert is_allowed_self_hosted_langfuse_url(url) is allowed


@pytest.mark.asyncio
async def test_observability_async_request_handles_cross_context_and_tracks_background_tokens(context_sessions) -> None:
    # Attach request_observations table to the in-memory SQLite AsyncEngine for ledger verification.
    engine = context_sessions.kw["bind"]
    meta = MetaData()
    Table(
        "request_observations",
        meta,
        Column("id", String, primary_key=True),
        Column("conversation_id", Integer),
        Column("user_message_id", Integer),
        Column("intent", String),
        Column("trace_id", String),
        Column("model_usage", JSON),
        Column("duration_ms", Integer),
        Column("status", String),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    )
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)

    cfg = fake_settings(openai_model="deepseek-chat", observability_enabled=True, langfuse_enabled=False)
    obs = Observability(cfg, context_sessions)

    # 1. Cross-context generator exit does not raise ValueError on ContextVar.reset.
    scope = obs.request(SimpleNamespace(raw_question="你好", conversation_id=999, user_id="u1"))
    record = await scope.__aenter__()
    other_ctx = contextvars.copy_context()
    # Exiting in a separate context copy triggers ValueError if ContextVar.reset is unguarded.
    fut = other_ctx.run(asyncio.ensure_future, scope.__aexit__(None, None, None))
    await fut
    await obs.save(record, SimpleNamespace(conversation_id=999, user_message_id=999))

    # 2. Background title generation reports token usage tagged with source="title".
    class CallbackReportingModel:
        model_name = "deepseek-chat"

        def with_structured_output(self, schema, **kwargs):
            async def _run(messages, config):
                run_id = uuid4()
                cbs = config.get("callbacks", [])
                handlers = getattr(cbs, "handlers", cbs)
                for cb in handlers:
                    if hasattr(cb, "on_llm_end"):
                        await cb.on_llm_end(
                            LLMResult(
                                generations=[
                                    [
                                        ChatGeneration(
                                            message=AIMessageChunk(
                                                content="ok",
                                                usage_metadata={"input_tokens": 42, "output_tokens": 6, "total_tokens": 48},
                                            )
                                        )
                                    ]
                                ]
                            ),
                            run_id=run_id,
                        )
                return {"title": "饮水机漏水换新"}

            return RunnableLambda(_run)

    user = str(uuid4())
    async with context_sessions() as session, session.begin():
        conv = await ConversationRepository(session).create(user)

    titles = ConversationTitles(cfg, context_sessions, CallbackReportingModel(), observability=obs)
    titles.schedule(conv.id, user, "我买的宠物自动饮水机收到后一直漏水想换一个新的")
    await asyncio.gather(*list(titles.tasks.values()))

    async with context_sessions() as session:
        saved_obs = list(
            await session.scalars(
                select(RequestObservation).where(RequestObservation.conversation_id == conv.id)
            )
        )
    assert len(saved_obs) == 1
    assert saved_obs[0].intent == "后台:会话标题"
    assert saved_obs[0].model_usage[0]["source"] == "title"
    assert saved_obs[0].model_usage[0]["input_tokens"] == 42
    assert saved_obs[0].model_usage[0]["output_tokens"] == 6


# ---------------------------------------------------------------------------
# 5. [P2] Deterministic query_product with is_demo=True (offline check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_product_is_deterministic_offline_and_marks_is_demo() -> None:
    rt1 = ToolRuntime(1, lambda: None, Random(1))
    rt2 = ToolRuntime(2, lambda: None, Random(999))
    t1 = next(t for t in build_business_tools(rt1) if t.name == "query_product")
    t2 = next(t for t in build_business_tools(rt2) if t.name == "query_product")

    res_a1 = json.loads(await t1.ainvoke({"product": "智能喂食器 Pro"}))
    res_a2 = json.loads(await t1.ainvoke({"product": "智能喂食器 Pro"}))
    res_b = json.loads(await t2.ainvoke({"product": "  智能喂食器   pro "}))
    res_other = json.loads(await t1.ainvoke({"product": "除臭猫砂盆"}))

    assert res_a1["is_demo"] is True
    assert res_a1["availability"] == res_a2["availability"] == res_b["availability"]
    assert res_a1["stock"] == res_a2["stock"] == res_b["stock"]
    assert res_other["is_demo"] is True
    assert isinstance(ChatService, type)


# ---------------------------------------------------------------------------
# 6. [A1 / A3 / A4] Policy hard-filter removal, section neighbors, timeouts,
#    DB pool sizing, mining JSON keys & orphan task cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent,question,content_type,section_path,answer_text",
    [
        (
            "售后",
            "智能喂食器保修期是多久？怎么寄修？",
            "manual",
            "售后与保养手册 / 保修期限与寄修流程",
            "智能硬件整机提供 12 个月保修。",
        ),
        (
            "退款退货",
            "飞轮审核补充的特殊赠品退货规则是什么？",
            "faq",
            "人工核准 FAQ / 特殊赠品退货规则",
            "随单赠品需与主商品一并退回。",
        ),
    ],
)
async def test_aftersales_and_refund_retrieve_manual_and_faq_without_policy_hard_filter(
    intent: str,
    question: str,
    content_type: str,
    section_path: str,
    answer_text: str,
) -> None:
    class ContentTypeAwareSearch:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def retrieve(self, query: str, **kwargs: object) -> RetrievalResult:
            self.calls.append(kwargs)
            allowed = kwargs.get("content_types")
            chunk = KnowledgeChunk(
                id=88,
                category="售后服务",
                questions=question,
                answer=answer_text,
                section_path=section_path,
                content_type=content_type,
                vectorize_status="done",
            )
            matches = (
                ()
                if allowed is not None and content_type not in allowed
                else (KnowledgeMatch(chunk, 0.92),)
            )
            return RetrievalResult(
                matches,
                QueryPlan(normalized_query=query),
                RetrievalStrategy.HYBRID_RERANK,
                bool(matches),
            )

    model = LabelledModel(
        intent,
        outputs=[f"{answer_text}[1]"],
        decision={"task_kind": "general_information"},
    )
    search = ContentTypeAwareSearch()
    service = InMemoryWorkflow(model, search)
    events = [event async for event in service.stream_turn(turn(question))]
    assert events[-1].name == "done"
    assert search.calls
    assert all("content_types" not in call_kwargs for call_kwargs in search.calls)
    assert model.expansion_calls == 1
    assert events[-1].data["sources"][0]["section_path"] == section_path
    assert answer_text in events[-1].data["content"]


@pytest.mark.asyncio
async def test_knowledge_repository_links_neighbors_only_within_same_section_path() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    meta = MetaData()
    Table(
        "knowledge_chunks",
        meta,
        Column("id", Integer, primary_key=True),
        Column("category", String),
        Column("questions", Text),
        Column("answer", Text),
        Column("section_path", String),
        Column("content_type", String),
        Column("source_kind", String, server_default=text("'manual'")),
        Column("source_path", String),
        Column("is_key_clause", Integer, server_default=text("0")),
        Column("prev_chunk_id", Integer),
        Column("next_chunk_id", Integer),
        Column("vector_id", String),
        Column("vectorize_status", String, server_default=text("'pending'")),
        Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    )
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        drafts = [
            KnowledgeDraft("售后政策", "退货条件", "第1段", section_path="售后政策 / 退货条件", content_type="policy"),
            KnowledgeDraft("售后政策", "退货条件", "第2段", section_path="售后政策 / 退货条件", content_type="policy"),
            KnowledgeDraft("MH-FD100", "清洁说明", "拆洗粮桶", section_path="MH-FD100 / 清洁保养", content_type="manual"),
            KnowledgeDraft("MH-LP100", "故障码", "E1说明", section_path="MH-LP100 / 故障排查", content_type="manual"),
            KnowledgeDraft("MH-LP100", "故障码", "E3说明", section_path="MH-LP100 / 故障排查", content_type="manual"),
        ]
        async with sessions() as session, session.begin():
            rows = await KnowledgeRepository(session).add_drafts(drafts, link_neighbors=True, source_kind="markdown")
            ids = [row.id for row in rows]
            neighbors = [(row.prev_chunk_id, row.next_chunk_id) for row in rows]

        assert neighbors == [
            (None, ids[1]),
            (ids[0], None),
            (None, None),
            (None, ids[4]),
            (ids[3], None),
        ]
    finally:
        await engine.dispose()


def test_conversation_extraction_prompt_keys_and_scoring_fingerprint_intact() -> None:
    assert "source_conversation_id" in CONVERSATION_KNOWLEDGE_EXTRACTION_SYSTEM_TEMPLATE
    rendered_system = str(render_conversation_knowledge_messages("[]")[0].content)
    assert "{items: [{source_conversation_id, question, answer}]}" in rendered_system
    repo_root = Path(__file__).resolve().parents[1]
    calibration_path = repo_root / "config" / "evidence-calibration.json"
    calibration_data = json.loads(calibration_path.read_text(encoding="utf-8"))
    cfg = fake_settings(evidence_calibration_path=str(calibration_path))
    assert scoring_fingerprint(repo_root / "knowledge", cfg) == calibration_data["scoring_fingerprint"]
    gate = EvidenceGate(cfg, repo_root / "knowledge")
    assert gate.check([], legacy_confident=False).calibrated is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("fast model timeout"),
        ConnectionError("connection reset"),
        httpx.ConnectTimeout("connect timed out"),
    ],
)
async def test_classify_intent_degrades_immediately_on_transport_or_timeout_error(error: Exception) -> None:
    calls = 0

    class FailingTransportModel:
        def with_structured_output(self, schema, *, method):
            class Runnable:
                async def ainvoke(self, messages):
                    nonlocal calls
                    calls += 1
                    raise error

            return Runnable()

    decision = await classify_intent(FailingTransportModel(), "json_mode", "查询订单1001物流")
    assert calls == 1
    assert decision.intent == "其他"
    assert decision.confidence == 0.0


@pytest.mark.asyncio
async def test_workflow_turn_timeout_emits_error_event_without_hanging(context_sessions) -> None:
    class HangingModel(LabelledModel):
        async def astream(self, messages):
            self.agent_calls += 1
            await asyncio.sleep(10)
            yield AIMessageChunk(content="永不到达")

    cfg = fake_settings(
        openai_model="deepseek-chat",
        mcp_enabled=False,
        workflow_turn_timeout_seconds=0.05,
    )
    service = WorkflowChatService(
        settings=cfg,
        model=HangingModel("订单"),
        session_factory=context_sessions,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=LabelledSearch(True),
    )
    try:
        prepared = await service.prepare_turn(ChatRequest(user_id=uuid4(), message="查订单1001状态"))
        events = await asyncio.wait_for(
            asyncio.create_task(_collect_stream(service, prepared)),
            timeout=2.0,
        )
        assert events[-1].name == "error"
        assert events[-1].data["code"] == "workflow_timeout"
    finally:
        await service.close()


async def _collect_stream(service: WorkflowChatService, prepared: PreparedTurn) -> list:
    return [event async for event in service.stream_turn(prepared)]


@pytest.mark.asyncio
async def test_agent_reminders_increment_agent_steps_and_respect_max_steps() -> None:
    model = LabelledModel(
        "物流",
        outputs=["旧缓存答复1", "旧缓存答复2"],
        decision={"task_kind": "progress_query", "requested_lookups": ["query_order", "query_logistics"]},
    )
    service = InMemoryWorkflow(model, LabelledSearch(True))
    service._settings = service._settings.model_copy(update={"max_agent_steps": 1})
    events = [event async for event in service.stream_turn(turn("先查订单1001状态再查物流"))]
    assert events[-1].name == "done"
    assert model.agent_calls == 2
    assert "本轮实时查询尚未完成" in events[-1].data["content"]
    snapshot = await service._checkpointer.aget_tuple({"configurable": {"thread_id": "demo:1"}})
    assert snapshot.checkpoint["channel_values"]["agent_reminders"] == 1
    assert snapshot.checkpoint["channel_values"]["agent_steps"] == 1


def test_async_engine_passes_explicit_pool_settings_on_mysql_and_skips_sqlite(monkeypatch) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def fake_create_async_engine(url: str, **kwargs: object):
        captured.append((url, kwargs))
        return SimpleNamespace(url=url, kwargs=kwargs)

    monkeypatch.setattr("app.database.session.create_async_engine", fake_create_async_engine)

    default_cfg = fake_settings(database_url="mysql+asyncmy://u:p@127.0.0.1:3306/pawhelp")
    create_async_engine_from_settings(default_cfg)
    assert captured[-1][1] == {
        "pool_pre_ping": True,
        "connect_args": {"init_command": MYSQL_UTC_INIT_COMMAND},
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 1800,
    }

    custom_cfg = fake_settings(
        database_url="mysql+asyncmy://u:p@127.0.0.1:3306/pawhelp",
        db_pool_size=24,
        db_max_overflow=48,
        db_pool_recycle_seconds=900,
    )
    create_async_engine_from_settings(custom_cfg)
    assert captured[-1][1]["pool_size"] == 24
    assert captured[-1][1]["max_overflow"] == 48
    assert captured[-1][1]["pool_recycle"] == 900

    sqlite_cfg = fake_settings(database_url="sqlite+aiosqlite:///:memory:")
    create_async_engine_from_settings(sqlite_cfg)
    assert "pool_size" not in captured[-1][1]
    assert "max_overflow" not in captured[-1][1]
    assert "pool_recycle" not in captured[-1][1]


@pytest.mark.asyncio
async def test_prepared_turn_has_no_dead_turn_lock_and_jev_shadow_reaps_cancelled_task(tmp_path) -> None:
    assert "turn_lock" not in {field.name for field in fields(PreparedTurn)}

    cal_path = tmp_path / "jev.json"
    cal_path.write_text(
        json.dumps(
            {
                "purpose": "jev_evidence_sufficiency",
                "model": "jev-1.14.0",
                "instructions_sha256": INSTRUCTIONS_SHA256,
                "lower": 0.2,
                "upper": 0.8,
                "validation": {"passed": True},
            }
        ),
        encoding="utf-8",
    )
    jev_cancelled = asyncio.Event()

    class SlowJevClient:
        async def system_one(self, state, questions, *, model=None, timeout=None):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                jev_cancelled.set()
                raise

    class FastLlm:
        async def check(self, question, evidence):
            await asyncio.sleep(0)
            return EvidenceDecision(useful=True, reason="LLM 判定充分")

    checker = JevRoutedChecker(
        FastLlm(),
        JevEvidenceJudge(SlowJevClient(), model="jev-1.14.0", timeout=5.0),
        mode="shadow",
        calibration_path=cal_path,
    )
    decision = await checker.check("包邮门槛是多少", [{"n": 1, "answer": "满99包邮"}])
    assert decision.useful is True
    assert decision.jev == {"error": "SlowerThanLlm"}
    assert jev_cancelled.is_set()

