"""voice group device lists

Revision ID: d1a5f7c9b2e4
Revises: c4d8e2f1a7b9
Create Date: 2026-11-05 15:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d1a5f7c9b2e4"
down_revision: str | None = "c4d8e2f1a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_group_list",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("device_type", sa.String(length=32), nullable=False),
        sa.Column("floor_id", sa.String(length=64), nullable=False),
        sa.Column("area_id", sa.String(length=64), nullable=False),
        sa.Column("entity_ids_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_voice_group_list_device_type"),
        "voice_group_list",
        ["device_type"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_voice_group_list_device_type"), table_name="voice_group_list"
    )
    op.drop_table("voice_group_list")
