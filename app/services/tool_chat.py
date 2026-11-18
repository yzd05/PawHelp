"""Lightweight turn contracts and shared response helpers for the chat workflow."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from langchain_core.tools import BaseTool

from app.chat import ChatEvent, StreamingChatModel
from app.context import PreparedContext
from app.schemas import ChatRequest
from app.tools.business import ToolRuntime


class ToolCallingChatModel(StreamingChatModel, Protocol):
    """A model that can create one tool-bound streaming selector."""

    def bind_tools(
        self, tools: Sequence[BaseTool], **kwargs: object
    ) -> StreamingChatModel: ...


ToolBuilder = Callable[[ToolRuntime], list[BaseTool]]
_CITATION_PATTERN = re.compile(r"\[(\d+)]")
_KNOWLEDGE_REFUSAL = "抱歉，当前知识库没有足够证据回答这个问题，我不会猜测。"
# A refusal should not be a dead end: offer the human handoff buttons with it.
_KNOWLEDGE_REFUSAL_ACTIONS = ("转人工", "建工单")


@dataclass(frozen=True)
class PreparedTurn:
    """Persisted and budgeted turn metadata with no live DB session or open lock."""

    conversation_id: int
    user_id: str
    user_message_id: int
    raw_question: str
    prepared_context: PreparedContext
    tool_runtime: ToolRuntime
    layered_context: bool = False


@runtime_checkable
class ChatService(Protocol):
    """Public conversational service boundary consumed by FastAPI routes."""

    async def prepare_turn(self, request: ChatRequest) -> PreparedTurn: ...

    def stream_turn(self, turn: PreparedTurn) -> AsyncIterator[ChatEvent]: ...


# Compatibility alias kept for external type annotations in app.main and app.api.chat.
ToolAwareChatService = ChatService


def citations_are_valid(
    answer: str, sources: Sequence[dict[str, object]]
) -> bool:
    """Require at least one valid [n] citation and forbid out-of-range indices."""

    allowed = {source.get("number") for source in sources}
    cited = {int(value) for value in _CITATION_PATTERN.findall(answer)}
    return bool(cited) and cited.issubset(allowed)


def build_error_event(*, code: str, message: str) -> ChatEvent:
    return ChatEvent("error", {"code": code, "message": message})


def build_done_event(
    turn: PreparedTurn,
    message_id: int,
    content: str,
    tool_trace: list[dict[str, object]],
    sources: list[dict[str, object]] | None = None,
    *,
    input_tokens_estimate: int | None = None,
    trimmed_messages: int | None = None,
) -> ChatEvent:
    return ChatEvent(
        "done",
        {
            "content": content,
            "conversation_id": turn.conversation_id,
            "message_id": message_id,
            "input_tokens_estimate": (
                turn.prepared_context.input_tokens_estimate
                if input_tokens_estimate is None
                else input_tokens_estimate
            ),
            "trimmed_messages": (
                turn.prepared_context.trimmed_messages
                if trimmed_messages is None
                else trimmed_messages
            ),
            "tool_trace": tool_trace,
            "sources": sources or [],
        },
    )


__all__ = [
    "_KNOWLEDGE_REFUSAL",
    "_KNOWLEDGE_REFUSAL_ACTIONS",
    "ChatService",
    "PreparedTurn",
    "ToolAwareChatService",
    "ToolBuilder",
    "ToolCallingChatModel",
    "build_done_event",
    "build_error_event",
    "citations_are_valid",
]
