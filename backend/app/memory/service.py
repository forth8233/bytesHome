from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Member, MemberPreference, UserFeedback, utcnow
from app.services.audit import add_audit


def _serialize_preference(row: MemberPreference) -> dict[str, Any]:
    return {
        "id": row.id,
        "member_id": row.member_id,
        "key": row.key,
        "value": row.value,
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
        self, member_id: int, key: str, value: str
    ) -> dict[str, Any]:
        row = await self._session.scalar(
            select(MemberPreference).where(
                MemberPreference.member_id == member_id, MemberPreference.key == key
            )
        )
        if row is None:
            row = MemberPreference(member_id=member_id, key=key, value=value)
            self._session.add(row)
        else:
            row.value = value
            row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return _serialize_preference(row)

    async def update_preference(
        self, member_id: int, pref_id: int, *, key: str | None, value: str | None
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
        if value is not None:
            row.value = value
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return _serialize_preference(row)

    async def delete_preference(self, member_id: int, pref_id: int) -> None:
        row = await self._require_preference(member_id, pref_id)
        await self._session.delete(row)
        await self._session.commit()

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
