"""Logical, transport-independent chat streaming events."""

from collections.abc import AsyncIterator, Sequence
from typing import NamedTuple, Protocol

from langchain_core.messages import AIMessageChunk, BaseMessage


class StreamingChatModel(Protocol):
    """The small streaming-model boundary used by the chat service."""

    def astream(
        self, messages: Sequence[BaseMessage]
    ) -> AsyncIterator[AIMessageChunk]: ...


class ChatEvent(NamedTuple):
    """One transport-independent event emitted by a chat service."""

    name: str
    data: dict[str, object]


def text_from_chunk(chunk: AIMessageChunk) -> str:
    """Extract displayable text while preserving an upstream chunk boundary."""

    content = chunk.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            text_parts.append(block["text"])
    return "".join(text_parts)
