"""scheduled task and run

Revision ID: 9a3b1d8c6e4f
Revises: f7a0c6e4b2d8
Create Date: 2026-11-10 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "9a3b1d8c6e4f"
down_revision: str | None = "f7a0c6e4b2d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_task",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=128), nullable=False),
        sa.Column("actions_json", sa.Text(), nullable=False),
        sa.Column("run_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=False),
        sa.Column("fired_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_scheduled_task_member_id"), "scheduled_task", ["member_id"]
    )
    op.create_index(
        op.f("ix_scheduled_task_run_at"), "scheduled_task", ["run_at"]
    )

    op.create_table(
        "scheduled_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("fired_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["scheduled_task.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_scheduled_run_task_id"), "scheduled_run", ["task_id"]
    )
    op.create_index(
        op.f("ix_scheduled_run_member_id"), "scheduled_run", ["member_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_scheduled_run_member_id"), table_name="scheduled_run")
    op.drop_index(op.f("ix_scheduled_run_task_id"), table_name="scheduled_run")
    op.drop_table("scheduled_run")

    op.drop_index(op.f("ix_scheduled_task_run_at"), table_name="scheduled_task")
    op.drop_index(op.f("ix_scheduled_task_member_id"), table_name="scheduled_task")
    op.drop_table("scheduled_task")