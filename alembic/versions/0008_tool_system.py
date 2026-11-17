"""Unified tool audit, no foreign keys and no destructive upgrade."""

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0008_tool_system"
down_revision = "0007_session_context_layers"
branch_labels = None
depends_on = None

_EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "conversation_id",
        "tool_call_id",
        "tool_name",
        "tool_source",
        "mcp_server",
        "arguments",
        "result_summary",
        "status",
        "error_message",
        "retry_count",
        "duration_ms",
        "created_at",
    }
)


def _create_tool_audit_logs() -> None:
    op.create_table(
        "tool_audit_logs",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("conversation_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("tool_call_id", mysql.VARCHAR(64), nullable=True),
        sa.Column("tool_name", mysql.VARCHAR(128), nullable=False),
        sa.Column("tool_source", mysql.ENUM("builtin", "mcp"), nullable=False),
        sa.Column("mcp_server", mysql.VARCHAR(64), nullable=True),
        sa.Column("arguments", mysql.JSON(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            mysql.ENUM("成功", "失败", "超时", "校验拦下", "权限拒绝"),
            nullable=False,
        ),
        sa.Column("error_message", mysql.VARCHAR(512), nullable=True),
        sa.Column(
            "retry_count",
            mysql.TINYINT(unsigned=True),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("duration_ms", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_conversation_id", "tool_audit_logs", ["conversation_id"])
    op.create_index("idx_tool_name", "tool_audit_logs", ["tool_name"])
    op.create_index("idx_status", "tool_audit_logs", ["status"])


def upgrade() -> None:
    if context.is_offline_mode():
        _create_tool_audit_logs()
        return

    inspector = sa.inspect(op.get_bind())
    if "tool_audit_logs" not in inspector.get_table_names():
        _create_tool_audit_logs()
    else:
        existing_cols = {c["name"] for c in inspector.get_columns("tool_audit_logs")}
        if _EXPECTED_COLUMNS - existing_cols:
            raise RuntimeError(
                "existing tool_audit_logs schema incomplete; preserve data and repair schema first"
            )
        if inspector.get_foreign_keys("tool_audit_logs"):
            raise RuntimeError("tool_audit_logs must not have foreign keys")


def downgrade() -> None:
    op.drop_table("tool_audit_logs")
