"""Non-destructive observability, exact-turn feedback and review persistence."""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0009_observability_flywheel"
down_revision = "0008_tool_system"
branch_labels = None
depends_on = None


def _review_queue_columns() -> list[sa.Column]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("normalized_question", mysql.VARCHAR(512), nullable=False),
        sa.Column("ai_suggested_answer", sa.Text(), nullable=True),
        sa.Column("occurrence_count", mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "review_status",
            mysql.ENUM("待审", "通过", "驳回"),
            nullable=False,
            server_default=sa.text("'待审'"),
        ),
        sa.Column("approved_answer", sa.Text(), nullable=True),
        sa.Column("published_chunk_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("publication_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    ]


def _eval_runs_columns() -> list[sa.Column]:
    return [
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "triggered_by",
            mysql.ENUM("定时", "手动"),
            nullable=False,
            server_default=sa.text("'定时'"),
        ),
        sa.Column("dataset_size", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("metrics", mysql.JSON(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def _request_observations_columns() -> list[sa.Column]:
    return [
        sa.Column("id", mysql.VARCHAR(36), primary_key=True, nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("user_message_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("intent", mysql.VARCHAR(32), nullable=False),
        sa.Column("trace_id", mysql.VARCHAR(64), nullable=True),
        sa.Column("model_usage", mysql.JSON(), nullable=False),
        sa.Column("duration_ms", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("status", mysql.VARCHAR(32), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def _create_frozen_table(name: str, columns: list[sa.Column], index_name: str, index_col: str) -> None:
    op.create_table(
        name,
        *columns,
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(index_name, name, [index_col])


def upgrade() -> None:
    op.execute("SET NAMES utf8mb4")
    frozen_tables = (
        ("review_queue", _review_queue_columns, "idx_review_status", "review_status"),
        ("eval_runs", _eval_runs_columns, "idx_created_at", "created_at"),
        ("request_observations", _request_observations_columns, "idx_observation_intent", "intent"),
    )
    extra_columns = {
        "messages": [
            sa.Column("reply_to_message_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("retrieved_chunks", mysql.JSON(), nullable=True),
            sa.Column("feedback", mysql.ENUM("up", "down"), nullable=True),
        ],
        "low_confidence_questions": [
            sa.Column("retrieved_chunks", mysql.JSON(), nullable=True),
            sa.Column("matched_review_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("event_key", mysql.VARCHAR(128), nullable=True),
        ],
    }

    if context.is_offline_mode():
        for name, factory, idx_name, idx_col in frozen_tables:
            _create_frozen_table(name, factory(), idx_name, idx_col)
        for table_name, additions in extra_columns.items():
            for column in additions:
                op.add_column(table_name, column)
        op.create_foreign_key(
            "fk_lcq_review",
            "low_confidence_questions",
            "review_queue",
            ["matched_review_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("idx_matched_review_id", "low_confidence_questions", ["matched_review_id"])
        op.create_index("uk_lcq_event", "low_confidence_questions", ["event_key"], unique=True)
        return

    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    for name, factory, idx_name, idx_col in frozen_tables:
        cols = factory()
        if name not in existing_tables:
            _create_frozen_table(name, cols, idx_name, idx_col)
        else:
            by_name = {c.name: c for c in cols}
            present = {c["name"] for c in sa.inspect(bind).get_columns(name)}
            for col_name in set(by_name) - present:
                col = by_name[col_name]
                if not col.nullable:
                    raise RuntimeError(
                        f"existing {name} missing required column {col_name}; preserve data"
                    )
                op.add_column(name, sa.Column(col_name, col.type, nullable=True))

    for table_name, additions in extra_columns.items():
        present = {c["name"] for c in sa.inspect(bind).get_columns(table_name)}
        for column in additions:
            if column.name not in present:
                op.add_column(table_name, column)

    inspector = sa.inspect(bind)
    if not any(
        f["constrained_columns"] == ["matched_review_id"]
        for f in inspector.get_foreign_keys("low_confidence_questions")
    ):
        op.create_foreign_key(
            "fk_lcq_review",
            "low_confidence_questions",
            "review_queue",
            ["matched_review_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not any(
        i["column_names"] == ["matched_review_id"]
        for i in inspector.get_indexes("low_confidence_questions")
    ):
        op.create_index("idx_matched_review_id", "low_confidence_questions", ["matched_review_id"])
    if not any(
        i["column_names"] == ["event_key"] and i["unique"]
        for i in inspector.get_indexes("low_confidence_questions")
    ):
        op.create_index("uk_lcq_event", "low_confidence_questions", ["event_key"], unique=True)


def downgrade() -> None:
    raise RuntimeError(
        "observability contains review and usage history; downgrade requires an explicit backup/export plan"
    )
