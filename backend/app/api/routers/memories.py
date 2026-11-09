from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.db.models import Member
from app.memory.service import MemoryService

router = APIRouter(prefix="/api/memories", tags=["memories"])

KEY_PATTERN = r"^[a-z_]+\.[^\s]{1,64}$"


class PreferenceIn(BaseModel):
    key: str = Field(min_length=3, max_length=128, pattern=KEY_PATTERN)
    value: str = Field(min_length=1, max_length=2000)
    value_type: str = Field(default="text", max_length=16)
    value_json: str | None = Field(default=None, max_length=4000)
    source: str = Field(default="manual", max_length=16)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    scope: str = Field(default="member", max_length=16)
    member_id: int | None = None


class PreferenceUpdate(BaseModel):
    key: str | None = Field(default=None, min_length=3, max_length=128, pattern=KEY_PATTERN)
    value: str | None = Field(default=None, min_length=1, max_length=2000)
    value_type: str | None = Field(default=None, max_length=16)
    value_json: str | None = Field(default=None, max_length=4000)
    source: str | None = Field(default=None, max_length=16)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    scope: str | None = Field(default=None, max_length=16)


class PurgeIn(BaseModel):
    confirm: bool = False
    member_id: int | None = None


def _target_member_id(user: Member, member_id: int | None) -> int:
    if member_id is None or member_id == user.id:
        return user.id
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator role required")
    return member_id


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


@router.get("/preference")
async def list_preferences(
    user: CurrentUser,
    session: DbSession,
    member_id: int | None = Query(default=None),
) -> list[dict[str, Any]]:
    target = _target_member_id(user, member_id)
    return await MemoryService(session).list_preferences(target)


@router.post("/preference", status_code=status.HTTP_201_CREATED)
async def create_preference(
    payload: PreferenceIn, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    target = _target_member_id(user, payload.member_id)
    return await MemoryService(session).upsert_preference(
        target,
        payload.key,
        payload.value,
        value_type=payload.value_type,
        value_json=payload.value_json,
        source=payload.source,
        confidence=payload.confidence,
        scope=payload.scope,
    )


@router.put("/preference/{pref_id}")
async def update_preference(
    pref_id: int, payload: PreferenceUpdate, user: CurrentUser, session: DbSession
) -> dict[str, Any]:
    if payload.key is None and payload.value is None and payload.value_json is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "nothing to update")
    return await MemoryService(session).update_preference(
        user.id,
        pref_id,
        key=payload.key,
        value=payload.value,
        value_type=payload.value_type,
        value_json=payload.value_json,
        source=payload.source,
        confidence=payload.confidence,
        scope=payload.scope,
    )


@router.delete("/preference/{pref_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preference(pref_id: int, user: CurrentUser, session: DbSession) -> None:
    await MemoryService(session).delete_preference(user.id, pref_id)


@router.get("/feedback")
async def list_feedback(
    user: CurrentUser,
    session: DbSession,
    member_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    target = _target_member_id(user, member_id)
    return await MemoryService(session).list_feedback(target, limit)


@router.post("/purge")
async def purge_memories(
    payload: PurgeIn, request: Request, user: AdminUser, session: DbSession
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "purge requires confirm=true"
        )
    if payload.member_id is not None and payload.member_id != user.id:
        existing = await session.get(Member, payload.member_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return await MemoryService(session).purge(
        user, target_member_id=payload.member_id, ip=_client_ip(request)
    )
