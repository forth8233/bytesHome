"""add source channel to scheduled_task

Revision ID: a5b1e9c7d3f2
Revises: d4e9f2c7b1a5
Create Date: 2026-11-10 14:20:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a5b1e9c7d3f2"
down_revision: str | None = "d4e9f2c7b1a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_task",
        sa.Column("source_channel", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "scheduled_task",
        sa.Column("source_ref", sa.String(length=128), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("scheduled_task", "source_ref")
    op.drop_column("scheduled_task", "source_channel")