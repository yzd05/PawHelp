"""Schema contracts for messages, knowledge and observability persistence."""

from sqlalchemy.dialects.mysql import JSON

from app.database.base import Base
from app.database.models import (
    FaithCase,
    KnowledgeChunk,
    LowConfidenceQuestion,
    Message,
    QAExtractionStaging,
    ReviewQueue,
)


def test_message_and_knowledge_foreign_data_contracts_are_preserved() -> None:
    assert "faq" not in Base.metadata.tables
    assert "faqs" not in Base.metadata.tables
    assert isinstance(Message.__table__.c.tool_calls.type, JSON)
    assert Message.__table__.c.content.nullable is True
    assert set(Message.__table__.c.keys()) == {
        "id",
        "conversation_id",
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "tool_trace",
        "suggested_actions",
        "source_refs",
        "related_order_id",
        "reply_to_message_id",
        "retrieved_chunks",
        "feedback",
        "created_at",
    }

    chunks = KnowledgeChunk.__table__
    assert next(iter(chunks.c.prev_chunk_id.foreign_keys)).ondelete == "SET NULL"
    assert next(iter(chunks.c.next_chunk_id.foreign_keys)).ondelete == "SET NULL"
    assert chunks.c.vectorize_status.type.enums == ["pending", "done", "failed", "deleting"]
    assert chunks.c.source_kind.nullable is False
    assert chunks.c.source_path.nullable is True

    review_fk = next(iter(ReviewQueue.__table__.c.published_chunk_id.foreign_keys))
    assert review_fk.target_fullname == "knowledge_chunks.id"
    assert review_fk.ondelete == "SET NULL"

    staging_indexed_cols = {
        tuple(col.name for col in index.columns)
        for index in QAExtractionStaging.__table__.indexes
    }
    assert ("source_ref",) in staging_indexed_cols

    assert LowConfidenceQuestion.__table__.c.source.type.enums == [
        "retrieval_low_conf",
        "self_check",
        "user_feedback",
    ]
    assert FaithCase.__table__.c.status.type.enums == ["未解决", "已解决", "无需解决"]
    assert FaithCase.__table__.c.eval_id.unique is True
