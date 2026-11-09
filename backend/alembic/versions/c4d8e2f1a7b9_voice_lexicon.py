"""voice lexicon, import batches and device profiles

Revision ID: c4d8e2f1a7b9
Revises: b7f3c1d2e4a5
Create Date: 2026-11-05 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c4d8e2f1a7b9"
down_revision: str | None = "b7f3c1d2e4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voice_lexicon_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lexicon", sa.String(length=32), nullable=False),
        sa.Column("term", sa.String(length=64), nullable=False),
        sa.Column("canonical", sa.String(length=128), nullable=False),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("functions_json", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("entry_type", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lexicon", "alias", name="uq_voice_lexicon_entry_alias"),
    )
    op.create_index(
        op.f("ix_voice_lexicon_entry_lexicon"), "voice_lexicon_entry", ["lexicon"]
    )
    op.create_index(
        op.f("ix_voice_lexicon_entry_alias"), "voice_lexicon_entry", ["alias"]
    )
    op.create_index(
        "ix_voice_lexicon_entry_lexicon_enabled",
        "voice_lexicon_entry",
        ["lexicon", "enabled"],
    )

    op.create_table(
        "voice_import_batch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("counts_json", sa.Text(), nullable=False),
        sa.Column("errors_json", sa.Text(), nullable=False),
        sa.Column("imported_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["imported_by"], ["member.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_voice_import_batch_sha256"), "voice_import_batch", ["sha256"]
    )

    op.create_table(
        "voice_device_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("device_type", sa.String(length=32), nullable=False),
        sa.Column("functions_json", sa.Text(), nullable=False),
        sa.Column("steps_json", sa.Text(), nullable=False),
        sa.Column("ranges_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id"),
    )
    op.create_index(
        op.f("ix_voice_device_profile_entity_id"),
        "voice_device_profile",
        ["entity_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_voice_device_profile_entity_id"),
        table_name="voice_device_profile",
    )
    op.drop_table("voice_device_profile")
    op.drop_index(
        op.f("ix_voice_import_batch_sha256"), table_name="voice_import_batch"
    )
    op.drop_table("voice_import_batch")
    op.drop_index(
        "ix_voice_lexicon_entry_lexicon_enabled", table_name="voice_lexicon_entry"
    )
    op.drop_index(
        op.f("ix_voice_lexicon_entry_alias"), table_name="voice_lexicon_entry"
    )
    op.drop_index(
        op.f("ix_voice_lexicon_entry_lexicon"), table_name="voice_lexicon_entry"
    )
    op.drop_table("voice_lexicon_entry")
