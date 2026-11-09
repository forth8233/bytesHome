from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminUser, DbSession
from app.db.models import (
    VoiceDeviceProfile,
    VoiceGroupList,
    VoiceLexiconEntry,
    utcnow,
)
from app.db.session import SessionFactory
from app.fastpath.capabilities import DEVICE_TYPE_LABELS
from app.fastpath.voice_import import import_workbook
from app.fastpath.voice_lexicon import LEXICONS
from app.services.audit import add_audit

router = APIRouter(prefix="/api/fastpath/voice", tags=["voice"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class EntryIn(BaseModel):
    lexicon: str = Field(min_length=1, max_length=32)
    term: str = Field(default="", max_length=64)
    canonical: str = Field(default="", max_length=128)
    alias: str = Field(min_length=1, max_length=64)
    functions: list[str] = Field(default_factory=list)
    unit: str = Field(default="", max_length=16)
    numeric_value: float | None = None


class EntryPatch(BaseModel):
    enabled: bool | None = None
    term: str | None = Field(default=None, max_length=64)
    canonical: str | None = Field(default=None, max_length=128)
    alias: str | None = Field(default=None, max_length=64)
    functions: list[str] | None = None
    unit: str | None = Field(default=None, max_length=16)
    numeric_value: float | None = None


class ProfileIn(BaseModel):
    device_type: str = Field(default="", max_length=32)
    functions: dict[str, Any] = Field(default_factory=dict)
    steps: dict[str, float] = Field(default_factory=dict)
    ranges: dict[str, list[float]] = Field(default_factory=dict)
    enabled: bool = True


class VoiceTestIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)


class GroupListIn(BaseModel):
    name: str = Field(default="", max_length=64)
    device_type: str = Field(min_length=1, max_length=32)
    floor_id: str = Field(default="", max_length=64)
    area_id: str = Field(default="", max_length=64)
    entity_ids: list[str] = Field(default_factory=list)
    enabled: bool = True


def _loads(raw: str | None, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _entry_out(row: VoiceLexiconEntry) -> dict[str, Any]:
    return {
        "id": row.id,
        "lexicon": row.lexicon,
        "term": row.term,
        "canonical": row.canonical,
        "alias": row.alias,
        "functions": _loads(row.functions_json, []),
        "unit": row.unit,
        "numeric_value": row.numeric_value,
        "entry_type": row.entry_type,
        "source": row.source,
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _profile_out(row: VoiceDeviceProfile) -> dict[str, Any]:
    return {
        "entity_id": row.entity_id,
        "device_type": row.device_type,
        "functions": _loads(row.functions_json, {}),
        "steps": _loads(row.steps_json, {}),
        "ranges": _loads(row.ranges_json, {}),
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _check_lexicon(name: str) -> str:
    if name not in LEXICONS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown lexicon: {name}",
        )
    return name


async def _reload_engine(request: Request) -> None:
    engine = getattr(request.app.state, "fast_path", None)
    if engine is not None:
        await engine.reload()


@router.get("/lexicons")
async def list_lexicons(_: AdminUser, session: DbSession) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                VoiceLexiconEntry.lexicon,
                func.count(VoiceLexiconEntry.id),
                func.sum(cast(VoiceLexiconEntry.enabled, Integer)),
            ).group_by(VoiceLexiconEntry.lexicon)
        )
    ).all()
    counts = {str(row[0]): {"total": int(row[1] or 0), "enabled": int(row[2] or 0)} for row in rows}
    items = [
        {
            "lexicon": name,
            "total": counts.get(name, {}).get("total", 0),
            "enabled": counts.get(name, {}).get("enabled", 0),
        }
        for name in LEXICONS
    ]
    return {"items": items, "total": sum(item["total"] for item in items)}


@router.get("/entries")
async def list_entries(
    _: AdminUser,
    session: DbSession,
    lexicon: str | None = Query(default=None, max_length=32),
    keyword: str | None = Query(default=None, max_length=64),
    enabled: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    if lexicon:
        _check_lexicon(lexicon)
    conditions = []
    if lexicon:
        conditions.append(VoiceLexiconEntry.lexicon == lexicon)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                VoiceLexiconEntry.alias.like(pattern),
                VoiceLexiconEntry.term.like(pattern),
                VoiceLexiconEntry.canonical.like(pattern),
            )
        )
    if enabled is not None:
        conditions.append(VoiceLexiconEntry.enabled.is_(enabled))
    base = select(VoiceLexiconEntry)
    if conditions:
        base = base.where(*conditions)
    total = (
        await session.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar_one()
    rows = (
        await session.scalars(
            base.order_by(VoiceLexiconEntry.lexicon, VoiceLexiconEntry.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return {
        "items": [_entry_out(row) for row in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.post("/entries", status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: EntryIn, admin: AdminUser, session: DbSession, request: Request
) -> dict[str, Any]:
    _check_lexicon(payload.lexicon)
    row = VoiceLexiconEntry(
        lexicon=payload.lexicon,
        term=payload.term or payload.alias,
        canonical=payload.canonical,
        alias=payload.alias,
        functions_json=json.dumps(payload.functions, ensure_ascii=False),
        unit=payload.unit,
        numeric_value=payload.numeric_value,
        entry_type=2,
        source="manual",
        enabled=True,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "alias already exists in this lexicon"
        ) from exc
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.entry_created",
        target=f"{payload.lexicon}:{payload.alias}"[:255],
        detail={"entry": _entry_out(row)},
    )
    await session.commit()
    await _reload_engine(request)
    return _entry_out(row)


@router.put("/entries/{entry_id}")
async def update_entry(
    entry_id: int,
    payload: EntryPatch,
    admin: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(VoiceLexiconEntry, entry_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entry not found")
    changes = payload.model_dump(exclude_unset=True)
    if row.entry_type != 2:
        extra = set(changes) - {"enabled"}
        if extra:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "system entries only allow enabling or disabling",
            )
    if "lexicon" in changes:
        changes.pop("lexicon")
    for key in ("enabled", "term", "canonical", "alias", "unit", "numeric_value"):
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    if isinstance(changes.get("functions"), list):
        row.functions_json = json.dumps(changes["functions"], ensure_ascii=False)
    row.updated_at = utcnow()
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.entry_updated",
        target=f"{row.lexicon}:{row.alias}"[:255],
        detail={"entry_id": row.id, "changes": changes},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "alias already exists in this lexicon"
        ) from exc
    await _reload_engine(request)
    return _entry_out(row)


@router.delete("/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: int, admin: AdminUser, session: DbSession, request: Request
) -> None:
    row = await session.get(VoiceLexiconEntry, entry_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entry not found")
    if row.entry_type != 2:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "system entries cannot be deleted",
        )
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.entry_deleted",
        target=f"{row.lexicon}:{row.alias}"[:255],
        detail={"entry": _entry_out(row)},
    )
    await session.delete(row)
    await session.commit()
    await _reload_engine(request)


@router.post("/import")
async def import_lexicon(
    admin: AdminUser,
    session: DbSession,
    request: Request,
    file: UploadFile,
    force: bool = False,
) -> dict[str, Any]:
    filename = Path(file.filename or "upload.xlsx").name
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "only .xlsx files are supported"
        )
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "workbook is too large"
        )
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / filename
        target.write_bytes(content)
        result = await import_workbook(
            target,
            session_factory=SessionFactory,
            imported_by=admin.id,
            force=force,
        )
    await _audit_import(session, admin, filename, result)
    if result.get("imported"):
        await _reload_engine(request)
    return result


async def _audit_import(session, admin, filename: str, result: dict[str, Any]) -> None:
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.imported",
        target=filename[:255],
        detail={
            key: result.get(key)
            for key in (
                "imported",
                "duplicate",
                "batch_id",
                "sha256",
                "entries",
                "source_rows",
                "alias_rows",
                "error_count",
            )
        },
    )
    await session.commit()


@router.post("/reload")
async def reload_lexicon(_: AdminUser, request: Request) -> dict[str, Any]:
    await _reload_engine(request)
    engine = getattr(request.app.state, "fast_path", None)
    return engine.stats() if engine is not None else {"voice_loaded": False}


@router.get("/profiles")
async def list_profiles(_: AdminUser, session: DbSession) -> dict[str, Any]:
    rows = (await session.scalars(select(VoiceDeviceProfile))).all()
    return {"items": [_profile_out(row) for row in rows]}


@router.put("/profiles/{entity_id}")
async def upsert_profile(
    entity_id: str,
    payload: ProfileIn,
    admin: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    if not entity_id or "." not in entity_id or len(entity_id) > 128:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid entity_id"
        )
    row = await session.scalar(
        select(VoiceDeviceProfile).where(
            VoiceDeviceProfile.entity_id == entity_id
        )
    )
    if row is None:
        row = VoiceDeviceProfile(entity_id=entity_id)
        session.add(row)
    row.device_type = payload.device_type
    row.functions_json = json.dumps(payload.functions, ensure_ascii=False)
    row.steps_json = json.dumps(payload.steps, ensure_ascii=False)
    row.ranges_json = json.dumps(payload.ranges, ensure_ascii=False)
    row.enabled = payload.enabled
    row.updated_at = utcnow()
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.profile_updated",
        target=entity_id[:255],
        detail={"profile": _profile_out(row)},
    )
    await session.commit()
    await _reload_engine(request)
    return _profile_out(row)


@router.delete("/profiles/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    entity_id: str, admin: AdminUser, session: DbSession, request: Request
) -> None:
    row = await session.scalar(
        select(VoiceDeviceProfile).where(
            VoiceDeviceProfile.entity_id == entity_id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "profile not found")
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.profile_deleted",
        target=entity_id[:255],
        detail={},
    )
    await session.delete(row)
    await session.commit()
    await _reload_engine(request)


@router.get("/directory")
async def voice_directory(_: AdminUser, request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "fast_path", None)
    if engine is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "fast path is not ready"
        )
    return await engine.voice_directory()


def _group_out(row: VoiceGroupList) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "device_type": row.device_type,
        "floor_id": row.floor_id,
        "area_id": row.area_id,
        "entity_ids": _loads(row.entity_ids_json, []),
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _check_device_type(name: str) -> str:
    if name not in DEVICE_TYPE_LABELS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown device type: {name}",
        )
    return name


@router.get("/group-lists")
async def list_group_lists(_: AdminUser, session: DbSession) -> dict[str, Any]:
    rows = (
        await session.scalars(select(VoiceGroupList).order_by(VoiceGroupList.id))
    ).all()
    return {"items": [_group_out(row) for row in rows]}


@router.post("/group-lists", status_code=status.HTTP_201_CREATED)
async def create_group_list(
    payload: GroupListIn, admin: AdminUser, session: DbSession, request: Request
) -> dict[str, Any]:
    _check_device_type(payload.device_type)
    row = VoiceGroupList(
        name=payload.name,
        device_type=payload.device_type,
        floor_id=payload.floor_id,
        area_id=payload.area_id,
        entity_ids_json=json.dumps(payload.entity_ids, ensure_ascii=False),
        enabled=payload.enabled,
    )
    session.add(row)
    await session.flush()
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.group_list_created",
        target=f"{payload.device_type}:{payload.name}"[:255],
        detail={"group_list": _group_out(row)},
    )
    await session.commit()
    await _reload_engine(request)
    return _group_out(row)


@router.put("/group-lists/{group_id}")
async def update_group_list(
    group_id: int,
    payload: GroupListIn,
    admin: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(VoiceGroupList, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group list not found")
    _check_device_type(payload.device_type)
    row.name = payload.name
    row.device_type = payload.device_type
    row.floor_id = payload.floor_id
    row.area_id = payload.area_id
    row.entity_ids_json = json.dumps(payload.entity_ids, ensure_ascii=False)
    row.enabled = payload.enabled
    row.updated_at = utcnow()
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.group_list_updated",
        target=f"{row.device_type}:{row.name}"[:255],
        detail={"group_list": _group_out(row)},
    )
    await session.commit()
    await _reload_engine(request)
    return _group_out(row)


@router.delete("/group-lists/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_list(
    group_id: int, admin: AdminUser, session: DbSession, request: Request
) -> None:
    row = await session.get(VoiceGroupList, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "group list not found")
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.group_list_deleted",
        target=f"{row.device_type}:{row.name}"[:255],
        detail={},
    )
    await session.delete(row)
    await session.commit()
    await _reload_engine(request)


@router.post("/test")
async def test_voice(
    payload: VoiceTestIn, admin: AdminUser, request: Request
) -> dict[str, Any]:
    engine = getattr(request.app.state, "fast_path", None)
    if engine is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "fast path is not ready"
        )
    result = await engine.test(payload.text, admin)
    return result.get("voice") or {
        "route": "slow_path",
        "reason": "voice_disabled_or_no_match",
    }
