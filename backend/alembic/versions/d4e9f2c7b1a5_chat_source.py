"""add source channel to chat_session

Revision ID: d4e9f2c7b1a5
Revises: 9a3b1d8c6e4f
Create Date: 2026-11-10 11:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d4e9f2c7b1a5"
down_revision: str | None = "9a3b1d8c6e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_session",
        sa.Column("channel", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "chat_session",
        sa.Column(
            "channel_ref", sa.String(length=128), nullable=False, server_default=""
        ),
    )
    op.create_index(
        op.f("ix_chat_session_channel"), "chat_session", ["channel"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chat_session_channel"), table_name="chat_session")
    op.drop_column("chat_session", "channel_ref")
    op.drop_column("chat_session", "channel")