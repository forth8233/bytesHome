"""channel identity binding

Revision ID: e2b6c8d3f5a1
Revises: d1a5f7c9b2e4
Create Date: 2026-11-07 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e2b6c8d3f5a1"
down_revision: str | None = "d1a5f7c9b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_identity",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("member_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("meta_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel", "external_id", name="uq_channel_identity_external"
        ),
    )
    op.create_index(
        op.f("ix_channel_identity_channel"), "channel_identity", ["channel"]
    )
    op.create_index(
        op.f("ix_channel_identity_member_id"), "channel_identity", ["member_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_channel_identity_member_id"), table_name="channel_identity")
    op.drop_index(op.f("ix_channel_identity_channel"), table_name="channel_identity")
    op.drop_table("channel_identity")
