from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime, default=utcnow, nullable=False)


class Member(Base):
    __tablename__ = "member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="member")
    notify_service: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = _ts()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()


class MemberPreference(Base):
    __tablename__ = "member_preference"
    __table_args__ = (UniqueConstraint("member_id", "key", name="uq_pref_member_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text)
    # 结构化偏好：domain 由 key 首段推导，value_json 承载与 value_type 一致的值
    domain: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    scope: Mapped[str] = mapped_column(String(16), default="member")
    value_type: Mapped[str] = mapped_column(String(16), default="text")
    value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = _ts()


class MemberEntityPermission(Base):
    __tablename__ = "member_entity_permission"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    entity_pattern: Mapped[str] = mapped_column(String(128))
    can_view: Mapped[bool] = mapped_column(Boolean, default=True)
    can_control: Mapped[bool] = mapped_column(Boolean, default=False)


class ProcessedEventLog(Base):
    __tablename__ = "processed_event_log"
    __table_args__ = (Index("ix_event_entity_time", "entity_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    entity_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(32))
    old_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_pre: Mapped[str] = mapped_column(String(16), default="low")
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dropped_count: Mapped[int] = mapped_column(Integer, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    received_at: Mapped[datetime] = _ts()


class Proposal(Base):
    __tablename__ = "proposal"
    __table_args__ = (
        Index("ix_proposal_status_expire", "status", "expire_at"),
        Index("ix_proposal_dedupe_status", "dedupe_key", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    event_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("processed_event_log.id", ondelete="SET NULL"), nullable=True
    )
    entity_id: Mapped[str] = mapped_column(String(128), default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(16), default="proposal")
    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    risk_source: Mapped[str] = mapped_column(String(16), default="code")
    thought: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    actions_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), nullable=True
    )
    expire_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    execution_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "action_signature", "event_type", name="uq_feedback_member_sig_event"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    action_signature: Mapped[str] = mapped_column(String(64), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_rejects: Mapped[int] = mapped_column(Integer, default=0)
    last_triggered_at: Mapped[datetime] = _ts()


class AutoPolicy(Base):
    __tablename__ = "auto_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    action_signature: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts()


class ReasoningTrace(Base):
    __tablename__ = "reasoning_trace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="proposal")
    provider: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class SystemConfig(Base):
    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    # 会话来源渠道："" = 网页/对话界面，feishu 等 = IM 渠道；channel_ref 是渠道内会话标识
    channel: Mapped[str] = mapped_column(String(16), default="", index=True)
    channel_ref: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_session.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = _ts()


class EntityAlias(Base):
    __tablename__ = "entity_alias"
    __table_args__ = (UniqueConstraint("alias", name="uq_entity_alias_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    alias: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    area_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain: Mapped[str] = mapped_column(String(32), default="")
    can_control: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IntentRule(Base):
    __tablename__ = "intent_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="scene")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    match_json: Mapped[str] = mapped_column(Text, default="{}")
    action_json: Mapped[str] = mapped_column(Text, default="[]")
    reply_template: Mapped[str] = mapped_column(String(255), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IntentRuleVersion(Base):
    __tablename__ = "intent_rule_version"
    __table_args__ = (UniqueConstraint("rule_id", "version", name="uq_intent_rule_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("intent_rule.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts()


class VoiceLexiconEntry(Base):
    __tablename__ = "voice_lexicon_entry"
    __table_args__ = (
        UniqueConstraint("lexicon", "alias", name="uq_voice_lexicon_entry_alias"),
        Index("ix_voice_lexicon_entry_lexicon_enabled", "lexicon", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lexicon: Mapped[str] = mapped_column(String(32), index=True)
    term: Mapped[str] = mapped_column(String(64), default="")
    canonical: Mapped[str] = mapped_column(String(128), default="")
    alias: Mapped[str] = mapped_column(String(64), index=True)
    functions_json: Mapped[str] = mapped_column(Text, default="[]")
    unit: Mapped[str] = mapped_column(String(16), default="")
    numeric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_type: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class VoiceImportBatch(Base):
    __tablename__ = "voice_import_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True, default="")
    counts_json: Mapped[str] = mapped_column(Text, default="{}")
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    imported_by: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts()


class VoiceDeviceProfile(Base):
    __tablename__ = "voice_device_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_type: Mapped[str] = mapped_column(String(32), default="")
    functions_json: Mapped[str] = mapped_column(Text, default="{}")
    steps_json: Mapped[str] = mapped_column(Text, default="{}")
    ranges_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class VoiceGroupList(Base):
    __tablename__ = "voice_group_list"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    device_type: Mapped[str] = mapped_column(String(32), index=True)
    floor_id: Mapped[str] = mapped_column(String(64), default="")
    area_id: Mapped[str] = mapped_column(String(64), default="")
    entity_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AutomationDraft(Base):
    __tablename__ = "automation_draft"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", ondelete="CASCADE"), index=True)
    confirm_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    yaml_content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = _ts()


class ChannelIdentity(Base):
    """IM 渠道账号与本地成员的绑定关系。"""

    __tablename__ = "channel_identity"
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_channel_identity_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(64), default="")
    # 各渠道的补充信息（飞书 chat_id / union_id、微信 iLink bot_id 等）
    meta_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ScheduledTask(Base):
    """语音/对话中解析出的定时任务（单次）。"""

    __tablename__ = "scheduled_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # relative/absolute/fuzzy/duration
    text: Mapped[str] = mapped_column(String(255))  # 原始指令文本
    summary: Mapped[str] = mapped_column(String(128))  # 用户可读描述
    actions_json: Mapped[str] = mapped_column(Text, default="[]")  # 规范化动作
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/cancelled/fired/failed
    result_text: Mapped[str] = mapped_column(Text, default="")
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = _ts()


class ScheduledRun(Base):
    """定时任务执行记录（审查）。"""

    __tablename__ = "scheduled_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_task.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16))  # ok/failed/skipped
    detail_json: Mapped[str] = mapped_column(Text, default="{}")  # 逐条结果/错误/guard 拒绝原因
    fired_at: Mapped[datetime] = _ts()
