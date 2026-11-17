"""Extend vectorize_status, index staging source_ref, bind published_chunk_id FK, drop dead faq table.

Revision ID: 0013_hardening_schema_cleanup
Revises: 0012_staging_discard_reason
"""

from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "0013_hardening_schema_cleanup"
down_revision: str | None = "0012_staging_discard_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "knowledge_chunks",
        "vectorize_status",
        existing_type=mysql.ENUM("pending", "done"),
        type_=mysql.ENUM("pending", "done", "failed", "deleting"),
        existing_nullable=False,
        nullable=False,
        server_default=sa.text("'pending'"),
    )

    op.execute(
        "UPDATE review_queue rq "
        "LEFT JOIN knowledge_chunks kc ON rq.published_chunk_id = kc.id "
        "SET rq.published_chunk_id = NULL "
        "WHERE rq.published_chunk_id IS NOT NULL AND kc.id IS NULL"
    )

    if context.is_offline_mode():
        op.create_index("idx_qa_staging_source_ref", "qa_extraction_staging", ["source_ref"])
        op.create_foreign_key(
            "fk_review_published_chunk",
            "review_queue",
            "knowledge_chunks",
            ["published_chunk_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.drop_table("faq")
        return

    inspector = sa.inspect(op.get_bind())
    staging_indexes = inspector.get_indexes("qa_extraction_staging")
    if not any(idx["column_names"] == ["source_ref"] for idx in staging_indexes):
        op.create_index("idx_qa_staging_source_ref", "qa_extraction_staging", ["source_ref"])

    review_fks = inspector.get_foreign_keys("review_queue")
    if not any(fk["constrained_columns"] == ["published_chunk_id"] for fk in review_fks):
        op.create_foreign_key(
            "fk_review_published_chunk",
            "review_queue",
            "knowledge_chunks",
            ["published_chunk_id"],
            ["id"],
            ondelete="SET NULL",
        )

    existing_tables = set(inspector.get_table_names())
    for dead_table in ("faq", "faqs"):
        if dead_table in existing_tables:
            op.drop_table(dead_table)


def downgrade() -> None:
    op.create_table(
        "faq",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False, comment="FAQ 主键"),
        sa.Column("question", mysql.VARCHAR(512), nullable=False, comment="问题"),
        sa.Column("answer", sa.Text(), nullable=False, comment="答案"),
        sa.Column("category", mysql.VARCHAR(64), nullable=False, comment="分类"),
        sa.Column(
            "created_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="常见问答",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_category", "faq", ["category"])
    op.drop_constraint("fk_review_published_chunk", "review_queue", type_="foreignkey")
    op.drop_index("idx_qa_staging_source_ref", table_name="qa_extraction_staging")
    op.execute("DELETE FROM knowledge_chunks WHERE vectorize_status = 'deleting'")
    op.execute(
        "UPDATE knowledge_chunks SET vectorize_status = 'pending' "
        "WHERE vectorize_status = 'failed'"
    )
    op.alter_column(
        "knowledge_chunks",
        "vectorize_status",
        existing_type=mysql.ENUM("pending", "done", "failed", "deleting"),
        type_=mysql.ENUM("pending", "done"),
        existing_nullable=False,
        nullable=False,
        server_default=sa.text("'pending'"),
    )
