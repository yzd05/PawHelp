"""Deterministic conversation workflow around the tool-aware agent node."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import json
import logging
from pathlib import Path
from random import Random
from typing import Annotated, Any, TypedDict
from weakref import WeakValueDictionary

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat import ChatEvent
from app.config import Settings
from app.context import ContextBudgetExceeded, PreparedContext
from app.database.models import ToolAuditLog
from app.knowledge.confidence import EvidenceGate
from app.knowledge.embedding import BgeM3Embedder
from app.knowledge.evidence import EvidenceSufficiencyChecker
from app.knowledge.jev_evidence import build_evidence_checker
from app.knowledge.pipeline import KnowledgeEvidencePipeline, QuestionSplitter
from app.knowledge.reranking import BgeReranker
from app.knowledge.search import HybridKnowledgeSearch, KnowledgeSearch
from app.knowledge.vector_store import MilvusKnowledgeStore
from app.llm import create_fast_chat_model
from app.observability import Observability, finish_trace
from app.prompts import WORKFLOW_AGENT_RULES, render_workflow_system_prompt
from app.repositories.conversations import ConversationRepository
from app.repositories.messages import MessageRepository
from app.repositories.quality import QualityRepository
from app.schemas import ChatRequest
from app.services.orders import OrderData
from app.services.tool_chat import (
    PreparedTurn,
    ToolBuilder,
    ToolCallingChatModel,
    build_done_event,
    build_error_event,
    citations_are_valid,
)
from app.tools.business import ToolRuntime, build_business_tools
from app.tools.catalog import ToolCatalog
from app.tools.registry import ToolRegistry
from app.workflow.memory import ConversationMemory
from app.workflow.nodes import TurnNodes, after_agent, after_confidence_gate, after_route, after_select_order
from app.workflow.titles import ConversationTitles
from app.workflow.tool_agent import ToolAgentNodes, ticket_requested
from app.workflow.understanding import Intent, Route

logger = logging.getLogger(__name__)

# Backward-compatible reference to the deduplicated workflow execution rules.
_AGENT_RULES = WORKFLOW_AGENT_RULES


class PendingWorkflowAction(ValueError):
    """An unfinished confirmation must not be overwritten by a new turn."""


class WorkflowState(TypedDict, total=False):
    raw_question: str
    user_message_id: int
    question: str
    messages: Annotated[list[BaseMessage], add_messages]
    context_version: int
    layered_context: bool
    input_tokens_estimate: int
    trimmed_messages: int
    intent: Intent
    intent_confidence: float
    task_kind: str
    required_tools: list[str]
    reference_status: str
    clarification_question: str
    routing_decision: str
    routing_reason: str
    needs_order: bool
    route: Route
    order_id: str
    order_observation: str
    refund_available: bool
    queries: list[str]
    evidence: list[dict[str, object]]
    retrieved_chunks: list[dict] | None
    evidence_confidence: dict
    confident: bool
    answer: str
    sources: list[dict[str, object]]
    trace: list[dict[str, object]]
    tool_records: list[tuple[dict[str, object], str]]
    actions: list[str]
    message_id: int
    pending_calls: list[dict]
    agent_steps: int
    agent_reminders: int
    agent_next: str
    ticket_requested: bool


class WorkflowChatService:
    """Production LangGraph conversational service with standalone persistence and scoped turn locks."""

    _citations_are_valid = staticmethod(citations_are_valid)
    _done_event = staticmethod(build_done_event)

    def __init__(
        self,
        *,
        settings: Settings,
        model: ToolCallingChatModel,
        session_factory: async_sessionmaker[AsyncSession],
        rng: Random | None = None,
        tool_builder: ToolBuilder = build_business_tools,
        knowledge_search: KnowledgeSearch | None = None,
        evidence_checker: EvidenceSufficiencyChecker | None = None,
        order_data: OrderData | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        catalog: Any = None,
        fast_model: Any = None,
    ) -> None:
        self._settings = settings
        self._model = model
        self._fast_model = (
            fast_model
            if fast_model is not None
            else (create_fast_chat_model(settings) if isinstance(model, ChatOpenAI) else model)
        )
        self._session_factory = session_factory
        self._rng = rng or Random()
        self._tool_builder = tool_builder
        self._orders = order_data or OrderData(session_factory)
        self._knowledge_search = knowledge_search or HybridKnowledgeSearch(
            session_factory,
            BgeM3Embedder(
                settings.embedding_model,
                batch_size=settings.embedding_batch_size,
                max_length=settings.embedding_max_length,
            ),
            MilvusKnowledgeStore(
                settings.milvus_uri,
                settings.milvus_hybrid_collection,
            ),
            BgeReranker(
                settings.reranker_model,
                batch_size=settings.reranker_batch_size,
                query_max_length=settings.reranker_query_max_length,
                passage_max_length=settings.reranker_passage_max_length,
            ),
            confidence_threshold=settings.knowledge_rerank_threshold,
            dense_limit=settings.dense_recall_limit,
            bm25_limit=settings.bm25_recall_limit,
            rerank_limit=settings.rerank_limit,
        )
        self._checkpointer = checkpointer or InMemorySaver()
        self._observability = Observability(self._settings, self._session_factory)
        self._evidence_gate = EvidenceGate(self._settings, Path(__file__).resolve().parents[2] / "knowledge")
        self._turn_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._catalog = catalog or ToolCatalog(self._settings, builder=self._tool_builder)
        runtime = ToolRuntime(0, self._session_factory, self._rng, self._knowledge_search, user_id="", order_data=self._orders)
        tools = [tool for tool in self._tool_builder(runtime) if tool.name not in {"query_logistics", "query_faq"}]
        self._startup_registry = ToolRegistry(tools)
        system = render_workflow_system_prompt(self._settings.shop_name)
        self._titles = ConversationTitles(
            self._settings,
            self._session_factory,
            self._fast_model,
            observability=self._observability,
        )
        self._memory = ConversationMemory(
            self._settings,
            self._session_factory,
            self._fast_model,
            system,
            json.dumps([convert_to_openai_tool(tool) for tool in tools], ensure_ascii=False),
            observability=self._observability,
        )
        active_checker = evidence_checker or build_evidence_checker(self._settings, self._fast_model)
        self._evidence_pipeline = KnowledgeEvidencePipeline(
            self._knowledge_search,
            self._evidence_gate,
            active_checker,
            QuestionSplitter(self._fast_model, method=self._settings.structured_output_method),
            top_k=self._settings.rerank_top_k,
            bound_text=lambda text: self._memory.bound_text(text, self._settings.evidence_chunk_max_tokens),
        )

    def validate_budget(self) -> None:
        self._memory.validate_budget()

    def is_summarizing(self, conversation_id: int) -> bool:
        return self._memory.is_summarizing(conversation_id)

    def _lock_for(self, user_id: str, conversation_id: int) -> asyncio.Lock:
        key = f"{user_id}:{conversation_id}"
        lock = self._turn_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._turn_locks[key] = lock
        return lock

    @asynccontextmanager
    async def _conversation_lock(self, user_id: str, conversation_id: int) -> AsyncIterator[None]:
        lock = self._lock_for(user_id, conversation_id)
        async with lock:
            yield

    async def prepare_turn(self, request: ChatRequest) -> PreparedTurn:
        """Validate budget and persist the user turn without holding open locks across generators."""

        self._memory.validate_budget()
        user_tokens = self._memory.count([HumanMessage(content=request.message)])
        if user_tokens > self._settings.max_user_input_tokens:
            raise ContextBudgetExceeded("current user message exceeds max_user_input_tokens")

        user_id = str(request.user_id)
        if request.conversation_id is None:
            conversation_id, user_message_id = await self._persist_user_turn(user_id, None, request.message)
        else:
            async with self._conversation_lock(user_id, request.conversation_id):
                if await self.pending_ticket(user_id, request.conversation_id):
                    raise PendingWorkflowAction("confirm or cancel the pending ticket first")
                conversation_id, user_message_id = await self._persist_user_turn(
                    user_id,
                    request.conversation_id,
                    request.message,
                )

        user_msg = HumanMessage(
            content=request.message,
            id=f"db-{user_message_id}",
            additional_kwargs={"db_message_id": user_message_id},
        )
        prepared = PreparedContext((user_msg,), user_tokens, 0)
        runtime = ToolRuntime(
            conversation_id,
            self._session_factory,
            self._rng,
            self._knowledge_search,
            user_id=user_id,
            order_data=self._orders,
        )
        return PreparedTurn(
            conversation_id=conversation_id,
            user_id=user_id,
            user_message_id=user_message_id,
            raw_question=request.message,
            prepared_context=prepared,
            tool_runtime=runtime,
            layered_context=True,
        )

    async def _persist_user_turn(
        self,
        user_id: str,
        conversation_id: int | None,
        message_text: str,
    ) -> tuple[int, int]:
        async with self._session_factory() as session, session.begin():
            conversations = ConversationRepository(session)
            conversation = (
                await conversations.create(user_id)
                if conversation_id is None
                else await conversations.get_owned(conversation_id, user_id)
            )
            row = await MessageRepository(session).add_user(conversation.id, message_text)
            return conversation.id, row.id

    async def _reconstruct_history_from_db(self, conversation_id: int) -> list[BaseMessage]:
        """Rebuild checkpoint history on cold start, recovering queried_orders from persisted traces/audit logs."""

        async with self._session_factory() as session:
            rows = await MessageRepository(session).list_ordered(conversation_id)
            audit_by_call_id: dict[str, dict[str, object]] = {}
            with suppress(SQLAlchemyError):
                audit_rows = list(
                    await session.scalars(
                        select(ToolAuditLog).where(
                            ToolAuditLog.conversation_id == conversation_id,
                            ToolAuditLog.tool_name == "query_order",
                            ToolAuditLog.status == "成功",
                        )
                    )
                )
                for audit in audit_rows:
                    if not audit.tool_call_id or not audit.result_summary:
                        continue
                    try:
                        parsed = json.loads(audit.result_summary)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(parsed, dict) and parsed.get("order_id") and not parsed.get("error") and not parsed.get("code"):
                        audit_by_call_id[audit.tool_call_id] = {
                            key: parsed[key] for key in ("order_id", "product", "item") if key in parsed
                        }

        rebuilt: list[BaseMessage] = []
        latest_user_id = 0
        for row in rows:
            if row.role == "user":
                latest_user_id = row.id
                rebuilt.append(
                    HumanMessage(
                        content=row.content or "",
                        id=f"db-{row.id}",
                        additional_kwargs={"db_message_id": row.id},
                    )
                )
            elif row.role == "tool":
                rebuilt.append(
                    ToolMessage(
                        content=row.content or "",
                        tool_call_id=row.tool_call_id or "",
                        id=f"db-{row.id}",
                        additional_kwargs={"db_message_id": row.id, "turn_message_id": latest_user_id},
                    )
                )
            else:
                turn_id = row.reply_to_message_id or latest_user_id
                for trace_item in row.tool_trace or []:
                    if not isinstance(trace_item, dict):
                        continue
                    if trace_item.get("status") != "succeeded" or trace_item.get("name") != "query_order":
                        continue
                    call_id = str(trace_item.get("call_id") or "")
                    if not call_id:
                        continue
                    order_payload = trace_item.get("observed_order")
                    if not isinstance(order_payload, dict) or not order_payload.get("order_id"):
                        order_payload = audit_by_call_id.get(call_id)
                    if not isinstance(order_payload, dict) or not order_payload.get("order_id"):
                        continue
                    call_spec = {
                        "id": call_id,
                        "name": "query_order",
                        "args": {"order_id": str(order_payload["order_id"])},
                    }
                    rebuilt.append(
                        AIMessage(
                            content="",
                            tool_calls=[call_spec],
                            id=f"reconstructed-ai-{row.id}-{call_id}",
                            additional_kwargs={"turn_message_id": turn_id},
                        )
                    )
                    rebuilt.append(
                        ToolMessage(
                            content=json.dumps(order_payload, ensure_ascii=False),
                            tool_call_id=call_id,
                            name="query_order",
                            id=f"reconstructed-tool-{row.id}-{call_id}",
                            additional_kwargs={"turn_message_id": turn_id},
                        )
                    )
                tool_calls = row.tool_calls if isinstance(row.tool_calls, list) else []
                rebuilt.append(
                    AIMessage(
                        content=row.content or "",
                        tool_calls=tool_calls,
                        id=f"db-{row.id}",
                        additional_kwargs={"db_message_id": row.id, "turn_message_id": turn_id},
                    )
                )
        return rebuilt

    async def _record_low_confidence(
        self,
        turn: PreparedTurn,
        *,
        source: str,
        reason: str,
        retrieved_chunks: list[dict] | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await QualityRepository(session).add_low_confidence(
                conversation_id=turn.conversation_id,
                raw_question=turn.raw_question,
                source=source,
                reason=reason,
                retrieved_chunks=retrieved_chunks,
                event_key=f"{source}:{turn.user_message_id}",
            )

    def _error_event(self, turn: PreparedTurn, *, code: str, message: str) -> ChatEvent:
        return build_error_event(code=code, message=message)

    async def close(self) -> None:
        await self._memory.close()
        await self._titles.close()
        await self._observability.close()

    def set_checkpointer(self, checkpointer: BaseCheckpointSaver) -> None:
        self._checkpointer = checkpointer

    def _build_graph(self, turn: PreparedTurn) -> tuple[Any, TurnNodes]:
        """Compile this turn's graph; nodes carry transient context, the graph only routes."""

        nodes = TurnNodes(self, turn)
        agent = ToolAgentNodes(self, turn, nodes)
        builder = StateGraph(WorkflowState)
        for name, node in {
            "resolve_reference": nodes.resolve_reference,
            "classify_intent": nodes.classify_intent,
            "route": nodes.route,
            "select_order": nodes.select_order,
            "retrieve": nodes.retrieve,
            "confidence_gate": nodes.confidence_gate,
            "agent": agent.step,
            "execute_tools": agent.execute,
            "complaint": nodes.complaint,
            "smalltalk": nodes.smalltalk,
            "other": nodes.other,
            "log": nodes.log_result,
        }.items():
            builder.add_node(name, node)
        builder.add_edge(START, "resolve_reference")
        builder.add_edge("resolve_reference", "classify_intent")
        builder.add_edge("classify_intent", "route")
        builder.add_conditional_edges(
            "route",
            after_route,
            {
                "knowledge": "retrieve",
                "order_knowledge": "select_order",
                "business": "agent",
                "select_order": "select_order",
                "complaint": "complaint",
                "smalltalk": "smalltalk",
                "other": "other",
            },
        )
        builder.add_conditional_edges("select_order", after_select_order)
        builder.add_edge("retrieve", "confidence_gate")
        builder.add_conditional_edges("confidence_gate", after_confidence_gate, {"agent": "agent", "log": "log"})
        builder.add_conditional_edges("agent", after_agent, {"agent": "agent", "execute_tools": "execute_tools", "log": "log"})
        builder.add_conditional_edges("execute_tools", after_agent, {"agent": "agent", "log": "log"})
        for node in ("complaint", "smalltalk", "other"):
            builder.add_edge(node, "log")
        builder.add_edge("log", END)
        return builder.compile(checkpointer=self._checkpointer), nodes

    async def _persist_workflow_answer(
        self,
        conversation_id: int,
        answer: str,
        actions: list[str],
        sources: list[dict[str, object]],
        *,
        related_order_id: str | None = None,
        tool_trace: list[dict[str, object]] | None = None,
        reply_to_message_id: int | None = None,
        retrieved_chunks: list[dict] | None = None,
    ) -> int:
        async with self._session_factory() as session, session.begin():
            row = await MessageRepository(session).add_assistant(
                conversation_id,
                answer,
                suggested_actions=actions,
                source_refs=sources,
                related_order_id=related_order_id,
                tool_trace=tool_trace,
                reply_to_message_id=reply_to_message_id,
                retrieved_chunks=retrieved_chunks,
            )
        return row.id

    async def stream_turn(self, turn: PreparedTurn) -> AsyncIterator[ChatEvent]:
        async with self._conversation_lock(turn.user_id, turn.conversation_id):
            async for event in self._stream_turn_locked(turn):
                yield event

    async def _stream_turn_locked(self, turn: PreparedTurn) -> AsyncIterator[ChatEvent]:
        yield ChatEvent("conversation", {"conversation_id": turn.conversation_id})
        graph, nodes = self._build_graph(turn)
        config = {"configurable": {"thread_id": f"{turn.user_id}:{turn.conversation_id}"}}
        checkpoint = await self._checkpointer.aget_tuple(config)
        values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
        incoming = [m for m in turn.prepared_context.messages if not isinstance(m, SystemMessage)]
        if values.get("context_version") != 7:
            # Cold checkpoint bootstrap or version upgrade: reconstruct canonical DB history once.
            if turn.layered_context:
                rebuilt = await self._reconstruct_history_from_db(turn.conversation_id)
                seen_ids = {m.id for m in rebuilt if m.id}
                for msg in incoming:
                    if msg.id not in seen_ids:
                        rebuilt.append(msg)
                delta: list[BaseMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *rebuilt]
            else:
                delta = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *incoming]
        else:
            ids = {m.id for m in values.get("messages", [])}
            delta = [m for m in incoming if m.id not in ids]

        initial: WorkflowState = {
            "raw_question": turn.raw_question,
            "user_message_id": turn.user_message_id,
            "question": turn.raw_question,
            "messages": delta,
            "context_version": 7,
            "layered_context": turn.layered_context,
            "input_tokens_estimate": turn.prepared_context.input_tokens_estimate,
            "trimmed_messages": turn.prepared_context.trimmed_messages,
            "evidence": [],
            "sources": [],
            "trace": [],
            "tool_records": [],
            "actions": [],
            "retrieved_chunks": None,
            "evidence_confidence": {},
            "answer": "",
            "confident": False,
            "intent": "其他",
            "intent_confidence": 0.0,
            "task_kind": "unspecified",
            "required_tools": [],
            "reference_status": "clear",
            "clarification_question": "",
            "routing_decision": "execute",
            "routing_reason": "",
            "needs_order": False,
            "order_id": "",
            "order_observation": "",
            "refund_available": False,
            "queries": [],
            "pending_calls": [],
            "agent_steps": 0,
            "agent_reminders": 0,
            "agent_next": "agent",
            "ticket_requested": ticket_requested(turn.raw_question, bool(values.get("ticket_requested", False))),
        }
        async for event in self._run_graph(graph, nodes, initial, turn):
            yield event

    async def _run_graph(
        self,
        graph: Any,
        nodes: TurnNodes,
        graph_input: object,
        turn: PreparedTurn,
    ) -> AsyncIterator[ChatEvent]:
        async with self._observability.request(turn) as record:
            try:
                async for event in self._run_observed_graph(graph, nodes, graph_input, turn, record):
                    if event.name == "error":
                        record["status"] = "failed"
                    yield event
            except BaseException:
                record["status"] = "cancelled"
                raise
            finally:
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(self._observability.save(record, turn))

    async def _run_observed_graph(
        self,
        graph: Any,
        nodes: TurnNodes,
        graph_input: object,
        turn: PreparedTurn,
        record: dict[str, Any],
    ) -> AsyncIterator[ChatEvent]:
        config = {"configurable": {"thread_id": f"{turn.user_id}:{turn.conversation_id}"}}
        try:
            async for item in graph.astream(
                graph_input,
                config={
                    **config,
                    "recursion_limit": 50,
                    "callbacks": record["callbacks"],
                    "metadata": {
                        "conversation_id": turn.conversation_id,
                        "user_message_id": turn.user_message_id,
                    },
                },
                stream_mode="custom",
                durability="sync",
            ):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    yield ChatEvent(item["name"], item.get("data", {}))
            snapshot = await graph.aget_state(config)
            finish_trace(record, snapshot.values)
            if snapshot.tasks:
                record["status"] = "interrupted"
            if turn.layered_context and not snapshot.tasks and snapshot.values.get("message_id"):
                memory = nodes.memory
                view = await memory.prepare(
                    turn.conversation_id,
                    turn.user_id,
                    snapshot.values["messages"],
                    snapshot.values["user_message_id"],
                )
                memory.schedule(turn.conversation_id, turn.user_id, view)
            # Greetings make poor titles; wait for the first substantive question.
            if not snapshot.tasks and snapshot.values.get("message_id") and snapshot.values.get("intent") != "闲聊":
                self._titles.schedule(turn.conversation_id, turn.user_id, snapshot.values["raw_question"])
            for task in snapshot.tasks:
                for pending in task.interrupts:
                    if isinstance(pending.value, dict) and pending.value.get("kind") == "select_order":
                        yield ChatEvent("order_selection", pending.value)
                    elif isinstance(pending.value, dict) and pending.value.get("kind") == "confirm_ticket":
                        yield ChatEvent("ticket_confirmation", pending.value)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("workflow execution failed conversation=%s", turn.conversation_id)
            yield self._error_event(turn, code="workflow_error", message="客服暂时无法处理，请稍后重试")

    async def _pending_order_context(
        self,
        user_id: str,
        conversation_id: int,
        kind: str = "select_order",
    ) -> tuple[PreparedTurn, tuple[Any, TurnNodes], dict[str, Any]] | None:
        config = {"configurable": {"thread_id": f"{user_id}:{conversation_id}"}}
        checkpoint = await self._checkpointer.aget_tuple(config)
        if checkpoint is None:
            return None
        values = checkpoint.checkpoint.get("channel_values", {})
        if not isinstance(values, dict) or not isinstance(values.get("raw_question"), str):
            return None
        messages = values.get("messages")
        if not isinstance(messages, list):
            return None
        raw_question = values["raw_question"]
        prepared = PreparedContext(
            tuple(messages),
            count_tokens_approximately(messages),
            0,
        )
        turn = PreparedTurn(
            conversation_id=conversation_id,
            user_id=user_id,
            user_message_id=int(values.get("user_message_id", 0)),
            raw_question=raw_question,
            prepared_context=prepared,
            tool_runtime=ToolRuntime(
                conversation_id,
                self._session_factory,
                self._rng,
                self._knowledge_search,
                user_id=user_id,
                order_data=self._orders,
            ),
            layered_context=bool(values.get("layered_context", False)),
        )
        graph, nodes = self._build_graph(turn)
        snapshot = await graph.aget_state(config)
        for task in snapshot.tasks:
            for pending in task.interrupts:
                if isinstance(pending.value, dict) and pending.value.get("kind") == kind:
                    return turn, (graph, nodes), pending.value
        return None

    async def pending_ticket(self, user_id: str, conversation_id: int) -> dict[str, Any] | None:
        context = await self._pending_order_context(user_id, conversation_id, "confirm_ticket")
        return context[2] if context else None

    async def resume_ticket(
        self,
        user_id: str,
        conversation_id: int,
        proposal_id: str,
        message_id: int,
        confirmed: bool,
    ) -> AsyncIterator[ChatEvent]:
        async with self._conversation_lock(user_id, conversation_id):
            context = await self._pending_order_context(user_id, conversation_id, "confirm_ticket")
            if (
                context is None
                or context[2].get("message_id") != message_id
                or context[2].get("proposal_id") != proposal_id
            ):
                raise ValueError("no matching pending ticket")
            turn, (graph, nodes), _ = context
            yield ChatEvent("conversation", {"conversation_id": conversation_id})
            async for event in self._run_graph(
                graph,
                nodes,
                Command(resume={"proposal_id": proposal_id, "confirmed": confirmed}),
                turn,
            ):
                yield event

    async def pending_order(self, user_id: str, conversation_id: int) -> dict[str, object] | None:
        context = await self._pending_order_context(user_id, conversation_id)
        return None if context is None else context[2]

    async def resume_order(
        self,
        user_id: str,
        conversation_id: int,
        order_id: str,
        message_id: int,
    ) -> AsyncIterator[ChatEvent]:
        async with self._conversation_lock(user_id, conversation_id):
            async for event in self._resume_order_locked(user_id, conversation_id, order_id, message_id):
                yield event

    async def _resume_order_locked(
        self,
        user_id: str,
        conversation_id: int,
        order_id: str,
        message_id: int,
    ) -> AsyncIterator[ChatEvent]:
        context = await self._pending_order_context(user_id, conversation_id)
        if (
            context is None
            or context[2].get("message_id") != message_id
            or order_id not in {str(order["order_id"]) for order in context[2].get("orders", [])}
        ):
            raise ValueError("no matching pending order")
        await self._orders.get_order(user_id, order_id)
        turn, (graph, nodes), _ = context
        yield ChatEvent("conversation", {"conversation_id": conversation_id})
        async for event in self._run_graph(graph, nodes, Command(resume=order_id), turn):
            yield event
