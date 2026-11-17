"""Core chat orchestration through the production WorkflowChatService seam."""

from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.database.models import (
    Conversation,
    KnowledgeChunk,
    LowConfidenceQuestion,
    Message,
    RequestObservation,
    Ticket,
    ToolAuditLog,
)
from app.knowledge.evidence import EvidenceDecision
from app.knowledge.query_understanding import QueryPlan
from app.knowledge.search import KnowledgeMatch, RetrievalResult, RetrievalStrategy
from app.schemas import ChatRequest
from app.services.tool_chat import ChatService
from app.workflow.graph import WorkflowChatService
from tests.order_fakes import FakeOrderData, TestToolCatalog
from tests.workflow_fakes import LabelledModel, tool_call


class FixedEvidenceChecker:
    def __init__(self, useful: bool = True) -> None:
        self.useful = useful

    async def check(self, question, evidence):
        return EvidenceDecision(useful=self.useful, reason="证据足够" if self.useful else "缺少答案")


class FixedKnowledgeSearch:
    def __init__(self, *, confident: bool) -> None:
        self.confident = confident

    async def retrieve(self, query, *, limit=10, **kwargs):
        row = KnowledgeChunk(
            id=7,
            questions="配送费用如何计算？",
            answer="配送费用会在结算页展示。",
            category="配送服务",
            section_path="配送服务 / 配送费用",
            content_type="faq",
            vectorize_status="done",
        )
        matches = (KnowledgeMatch(row, 0.93),) if self.confident else ()
        return RetrievalResult(
            matches,
            QueryPlan(normalized_query=query),
            RetrievalStrategy.HYBRID_RERANK,
            self.confident,
        )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        chat_base_url="http://localhost:11434/v1",
        openai_model="qwen3",
        openai_api_key="ollama",
        database_url="mysql+asyncmy://unused",
        shop_name="测试店",
        tool_timeout_seconds=1,
        tool_max_attempts=1,
        mcp_enabled=False,
        observability_enabled=False,
        evidence_calibration_path=".scratch/nonexistent-calibration.json",
    )


@pytest.fixture
async def database(mysql_session):
    factory = async_sessionmaker(mysql_session.bind, expire_on_commit=False)
    conversation_ids: list[int] = []
    yield factory, conversation_ids
    if conversation_ids:
        async with factory() as session, session.begin():
            await session.execute(
                delete(RequestObservation).where(RequestObservation.conversation_id.in_(conversation_ids))
            )
            await session.execute(
                delete(ToolAuditLog).where(ToolAuditLog.conversation_id.in_(conversation_ids))
            )
            await session.execute(
                delete(Message).where(Message.conversation_id.in_(conversation_ids))
            )
            await session.execute(
                delete(Ticket).where(Ticket.conversation_id.in_(conversation_ids))
            )
            await session.execute(
                delete(LowConfidenceQuestion).where(
                    LowConfidenceQuestion.conversation_id.in_(conversation_ids)
                )
            )
            await session.execute(
                delete(Conversation).where(Conversation.id.in_(conversation_ids))
            )


async def _events(service: ChatService, request: ChatRequest):
    turn = await service.prepare_turn(request)
    events = [event async for event in service.stream_turn(turn)]
    return turn, events


async def test_new_and_followup_turns_share_database_generated_context(database) -> None:
    factory, conversation_ids = database
    model = LabelledModel("订单", ["第一轮回答", "第二轮回答"])
    service = WorkflowChatService(
        settings=_settings(),
        model=model,
        session_factory=factory,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=FixedKnowledgeSearch(confident=True),
    )
    assert isinstance(service, ChatService)
    user_id = uuid4()

    try:
        first, first_events = await _events(
            service, ChatRequest(user_id=user_id, message="订单1001第一轮问题")
        )
        conversation_ids.append(first.conversation_id)
        second, second_events = await _events(
            service,
            ChatRequest(
                user_id=user_id,
                conversation_id=first.conversation_id,
                message="订单1001接着问",
            ),
        )

        assert isinstance(first.conversation_id, int)
        assert first_events[0].data == {"conversation_id": first.conversation_id}
        assert second.conversation_id == first.conversation_id
        seen_human = [
            message.content
            for message in model.agent_histories[-1]
            if isinstance(message, HumanMessage)
        ]
        assert "订单1001第一轮问题" in seen_human
        assert "订单1001接着问" in seen_human
        assert second_events[-1].name == "done"
    finally:
        await service.close()


async def test_tool_call_trace_is_persisted_and_converges(database) -> None:
    factory, conversation_ids = database
    model = LabelledModel(
        "物流",
        [
            tool_call("query_logistics", '{"order_id":"1001"}', "call-1"),
            "包裹正在运输中。",
        ],
    )
    service = WorkflowChatService(
        settings=_settings(),
        model=model,
        session_factory=factory,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=FixedKnowledgeSearch(confident=True),
    )

    try:
        turn, events = await _events(
            service, ChatRequest(user_id=uuid4(), message="订单 1001 到哪了")
        )
        conversation_ids.append(turn.conversation_id)

        assert events[0].name == "conversation"
        assert [e.data["status"] for e in events if e.name == "tool_status"] == ["running", "succeeded"]
        assert events[-1].name == "done"
        assert events[-1].data["content"] == "包裹正在运输中。"
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == turn.conversation_id)
                    .order_by(Message.id)
                )
            )
        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[-1].tool_trace[0]["name"] == "query_logistics"
    finally:
        await service.close()


async def test_model_failure_returns_a_safe_error_without_closing_conversation(
    database,
) -> None:
    factory, conversation_ids = database

    class FailingAgentModel(LabelledModel):
        async def astream(self, messages):
            raise RuntimeError("secret")
            yield  # pragma: no cover

    model = FailingAgentModel("订单")
    service = WorkflowChatService(
        settings=_settings(),
        model=model,
        session_factory=factory,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=FixedKnowledgeSearch(confident=True),
    )

    try:
        turn, events = await _events(
            service, ChatRequest(user_id=uuid4(), message="订单1001状态")
        )
        conversation_ids.append(turn.conversation_id)

        assert events[-1].name == "error"
        assert "secret" not in str(events[-1].data)
        async with factory() as session:
            conversation = await session.get(Conversation, turn.conversation_id)
        assert conversation is not None
        assert conversation.status == "进行中"
    finally:
        await service.close()


async def test_knowledge_answer_returns_validated_citation_sources(database) -> None:
    factory, conversation_ids = database
    model = LabelledModel(
        "商品咨询",
        ["运费请以结算页为准 [1]。"],
        decision={"task_kind": "general_information"},
    )
    service = WorkflowChatService(
        settings=_settings(),
        model=model,
        session_factory=factory,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=FixedKnowledgeSearch(confident=True),
        evidence_checker=FixedEvidenceChecker(useful=True),
    )

    try:
        turn, events = await _events(
            service, ChatRequest(user_id=uuid4(), message="邮费是多少")
        )
        conversation_ids.append(turn.conversation_id)

        done = events[-1]
        assert done.name == "done"
        assert done.data["content"] == "运费请以结算页为准 [1]。"
        assert done.data["sources"] == [
            {
                "number": 1,
                "chunk_id": 7,
                "category": "配送服务",
                "section_path": "配送服务 / 配送费用",
            }
        ]
    finally:
        await service.close()


async def test_low_confidence_knowledge_refuses_and_enters_question_pool(database) -> None:
    factory, conversation_ids = database
    model = LabelledModel(
        "商品咨询",
        decision={"task_kind": "general_information"},
    )
    service = WorkflowChatService(
        settings=_settings(),
        model=model,
        session_factory=factory,
        order_data=FakeOrderData(),
        catalog=TestToolCatalog(),
        knowledge_search=FixedKnowledgeSearch(confident=False),
        evidence_checker=FixedEvidenceChecker(useful=True),
    )

    try:
        turn, events = await _events(
            service, ChatRequest(user_id=uuid4(), message="会员积分什么时候到账")
        )
        conversation_ids.append(turn.conversation_id)

        assert events[-1].name == "done"
        assert "不会猜测" in events[-1].data["content"]
        assert model.agent_calls == 0
        async with factory() as session:
            row = await session.scalar(
                select(LowConfidenceQuestion).where(
                    LowConfidenceQuestion.conversation_id == turn.conversation_id
                )
            )
        assert row is not None
        assert row.source == "retrieval_low_conf"
        assert row.raw_question == "会员积分什么时候到账"
    finally:
        await service.close()
