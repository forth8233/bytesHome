"""fast path rules and aliases

Revision ID: b7f3c1d2e4a5
Revises: 39bceb5eaeb9
Create Date: 2026-11-04 18:30:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "b7f3c1d2e4a5"
down_revision: str | None = "39bceb5eaeb9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_alias",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("alias", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("area_id", sa.String(length=64), nullable=True),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column("can_control", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alias", name="uq_entity_alias_alias"),
    )
    op.create_index(op.f("ix_entity_alias_entity_id"), "entity_alias", ["entity_id"])
    op.create_index(op.f("ix_entity_alias_alias"), "entity_alias", ["alias"])

    op.create_table(
        "intent_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("match_json", sa.Text(), nullable=False),
        sa.Column("action_json", sa.Text(), nullable=False),
        sa.Column("reply_template", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["member.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "intent_rule_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["changed_by"], ["member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["intent_rule.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "version", name="uq_intent_rule_version"),
    )
    op.create_index(
        op.f("ix_intent_rule_version_rule_id"), "intent_rule_version", ["rule_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_intent_rule_version_rule_id"), table_name="intent_rule_version")
    op.drop_table("intent_rule_version")
    op.drop_table("intent_rule")
    op.drop_index(op.f("ix_entity_alias_alias"), table_name="entity_alias")
    op.drop_index(op.f("ix_entity_alias_entity_id"), table_name="entity_alias")
    op.drop_table("entity_alias")
