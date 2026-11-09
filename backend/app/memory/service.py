from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Member, MemberPreference, UserFeedback, utcnow
from app.services.audit import add_audit

# 值类型白名单
VALID_VALUE_TYPES = {
    "text",
    "number",
    "number_range",
    "boolean",
    "string_enum",
    "json",
}
VALID_SOURCES = {"manual", "llm", "rule", "voice"}
VALID_SCOPES = {"member", "home", "global"}


def _derive_domain(key: str) -> str:
    return key.split(".", 1)[0] if "." in key else key


def _validate_value(value_type: str, value_json: str | None) -> None:
    """校验 value_json 是否与 value_type 匹配；todo 处理导入的场景."""
    if value_json is None or value_json == "":
        return  # 仅文本偏好允许空结构化值
    try:
        parsed = json.loads(value_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"value_json 不是合法 JSON: {exc.msg}",
        ) from exc
    if value_type == "number":
        if not isinstance(parsed, (int, float)) or isinstance(parsed, bool):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "value_type=number 需要数字 value_json",
            )
    elif value_type == "number_range":
        if (
            not isinstance(parsed, list)
            or len(parsed) != 2
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in parsed)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "value_type=number_range 需要 [lo, hi] 形式 JSON 数组",
            )
    elif value_type == "boolean":
        if not isinstance(parsed, bool):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "value_type=boolean 需要 true/false JSON",
            )
    elif value_type == "json":
        # 任意合法 JSON 均可
        pass


def _serialize_preference(row: MemberPreference) -> dict[str, Any]:
    return {
        "id": row.id,
        "member_id": row.member_id,
        "key": row.key,
        "value": row.value,
        "domain": row.domain,
        "source": row.source,
        "confidence": row.confidence,
        "scope": row.scope,
        "value_type": row.value_type,
        "value_json": row.value_json,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _serialize_feedback(row: UserFeedback) -> dict[str, Any]:
    return {
        "id": row.id,
        "member_id": row.member_id,
        "event_type": row.event_type,
        "action_signature": row.action_signature,
        "decision": row.decision,
        "count": row.count,
        "consecutive_rejects": row.consecutive_rejects,
        "last_triggered_at": row.last_triggered_at.isoformat()
        if row.last_triggered_at
        else None,
    }


class MemoryService:
    """结构化长期记忆：成员偏好与反馈，只经 API 读写，不落向量库。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_preferences(self, member_id: int) -> list[dict[str, Any]]:
        rows = (
            await self._session.scalars(
                select(MemberPreference)
                .where(MemberPreference.member_id == member_id)
                .order_by(MemberPreference.key)
            )
        ).all()
        return [_serialize_preference(row) for row in rows]

    async def upsert_preference(
        self,
        member_id: int,
        key: str,
        value: str,
        *,
        value_type: str = "text",
        value_json: str | None = None,
        source: str = "manual",
        confidence: float = 1.0,
        scope: str = "member",
    ) -> dict[str, Any]:
        if value_type not in VALID_VALUE_TYPES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知 value_type")
        if source not in VALID_SOURCES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知 source")
        if scope not in VALID_SCOPES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知 scope")
        if not (0.0 <= confidence <= 1.0):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "confidence 需在 0~1")
        _validate_value(value_type, value_json)

        row = await self._session.scalar(
            select(MemberPreference).where(
                MemberPreference.member_id == member_id, MemberPreference.key == key
            )
        )
        if row is None:
            row = MemberPreference(
                member_id=member_id,
                key=key,
                value=value,
                domain=_derive_domain(key),
                value_type=value_type,
                value_json=value_json,
                source=source,
                confidence=confidence,
                scope=scope,
            )
            self._session.add(row)
        else:
            row.value = value
            row.domain = _derive_domain(key)
            row.value_type = value_type
            row.value_json = value_json
            row.source = source
            row.confidence = confidence
            row.scope = scope
            row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return _serialize_preference(row)

    async def update_preference(
        self,
        member_id: int,
        pref_id: int,
        *,
        key: str | None = None,
        value: str | None = None,
        value_type: str | None = None,
        value_json: str | None = None,
        source: str | None = None,
        confidence: float | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        row = await self._require_preference(member_id, pref_id)
        if key is not None and key != row.key:
            clash = await self._session.scalar(
                select(MemberPreference).where(
                    MemberPreference.member_id == member_id,
                    MemberPreference.key == key,
                    MemberPreference.id != pref_id,
                )
            )
            if clash is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "preference key already exists")
            row.key = key
            row.domain = _derive_domain(key)
        if value is not None:
            row.value = value
        if value_type is not None:
            if value_type not in VALID_VALUE_TYPES:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知 value_type")
            row.value_type = value_type
            _validate_value(row.value_type, row.value_json if value_json is None else value_json)
        if value_json is not None:
            _validate_value(row.value_type, value_json)
            row.value_json = value_json
        if source is not None:
            if source not in VALID_SOURCES:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知 source")
            row.source = source
        if confidence is not None:
            if not (0.0 <= confidence <= 1.0):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "confidence 需在 0~1")
            row.confidence = confidence
        if scope is not None:
            if scope not in VALID_SCOPES:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知 scope")
            row.scope = scope
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return _serialize_preference(row)

    async def delete_preference(self, member_id: int, pref_id: int) -> None:
        row = await self._require_preference(member_id, pref_id)
        await self._session.delete(row)
        await self._session.commit()

    async def delete_preference_by_key(self, member_id: int, key: str) -> bool:
        """按 key 删除偏好（供自动记忆通道 withdraw 使用），返回是否存在。"""
        row = await self._session.scalar(
            select(MemberPreference).where(
                MemberPreference.member_id == member_id, MemberPreference.key == key
            )
        )
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

    async def list_feedback(self, member_id: int, limit: int = 100) -> list[dict[str, Any]]:
        rows = (
            await self._session.scalars(
                select(UserFeedback)
                .where(UserFeedback.member_id == member_id)
                .order_by(UserFeedback.last_triggered_at.desc())
                .limit(limit)
            )
        ).all()
        return [_serialize_feedback(row) for row in rows]

    async def purge(
        self,
        actor: Member,
        *,
        target_member_id: int | None,
        ip: str = "",
    ) -> dict[str, Any]:
        """清空记忆需要二次确认；未指定成员时清空全部成员。"""
        pref_filters = [] if target_member_id is None else [MemberPreference.member_id == target_member_id]
        feedback_filters = [] if target_member_id is None else [UserFeedback.member_id == target_member_id]

        pref_count = await self._session.scalar(
            select(func.count()).select_from(MemberPreference).where(*pref_filters)
        )
        feedback_count = await self._session.scalar(
            select(func.count()).select_from(UserFeedback).where(*feedback_filters)
        )
        await self._session.execute(delete(MemberPreference).where(*pref_filters))
        await self._session.execute(delete(UserFeedback).where(*feedback_filters))
        add_audit(
            self._session,
            member_id=actor.id,
            action="memory.purge",
            target="all" if target_member_id is None else f"member:{target_member_id}",
            detail={
                "preferences": int(pref_count or 0),
                "feedback": int(feedback_count or 0),
                "scope": "all" if target_member_id is None else "member",
            },
            ip=ip,
        )
        await self._session.commit()
        return {
            "purged_preferences": int(pref_count or 0),
            "purged_feedback": int(feedback_count or 0),
            "scope": "all" if target_member_id is None else "member",
            "member_id": target_member_id,
        }

    async def _require_preference(self, member_id: int, pref_id: int) -> MemberPreference:
        row = await self._session.get(MemberPreference, pref_id)
        if row is None or row.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "preference not found")
        return row
