"""structure member_preference

Revision ID: f7a0c6e4b2d8
Revises: e2b6c8d3f5a1
Create Date: 2026-11-09 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f7a0c6e4b2d8"
down_revision: str | None = "e2b6c8d3f5a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "member_preference",
        sa.Column("domain", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "member_preference",
        sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
    )
    op.add_column(
        "member_preference",
        sa.Column(
            "confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")
        ),
    )
    op.add_column(
        "member_preference",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="member"),
    )
    op.add_column(
        "member_preference",
        sa.Column(
            "value_type", sa.String(length=16), nullable=False, server_default="text"
        ),
    )
    op.add_column(
        "member_preference",
        sa.Column("value_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("member_preference", "value_json")
    op.drop_column("member_preference", "value_type")
    op.drop_column("member_preference", "scope")
    op.drop_column("member_preference", "confidence")
    op.drop_column("member_preference", "source")
    op.drop_column("member_preference", "domain")