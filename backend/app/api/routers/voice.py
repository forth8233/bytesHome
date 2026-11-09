from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
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

# 词库导入模板，随本后端打包（COPY app）提供下载
VOICE_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "fastpath" / "voice_template.xlsx"


@router.get("/template")
async def download_template() -> FileResponse:
    """下载语音词库导入模板（.xlsx），与解析器列结构对齐。"""
    if not VOICE_TEMPLATE_PATH.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found")
    return FileResponse(
        VOICE_TEMPLATE_PATH,
        filename="语音词库导入模板.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


class EntryIn(BaseModel):
    lexicon: str = Field(min_length=1, max_length=32)
    term: str = Field(default="", max_length=64)
    canonical: str = Field(default="", max_length=128)
    alias: str = Field(default="", max_length=64)
    aliases: list[str] = Field(default_factory=list)
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


class EntryGroupIn(BaseModel):
    """按词（grouped by lexicon+term）整组编辑。别名集合整体替换。"""

    lexicon: str = Field(min_length=1, max_length=32)
    term: str = Field(min_length=1, max_length=64)
    new_term: str | None = Field(default=None, max_length=64)
    canonical: str | None = Field(default=None, max_length=128)
    aliases: list[str] | None = None
    functions: list[str] | None = None
    unit: str | None = Field(default=None, max_length=16)
    numeric_value: float | None = None
    enabled: bool | None = None


class EntryGroupDeleteIn(BaseModel):
    """按词（grouped by lexicon+term）整组删除。"""

    lexicon: str = Field(min_length=1, max_length=32)
    term: str = Field(min_length=1, max_length=64)


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


def _group_out(rows: list[VoiceLexiconEntry]) -> dict[str, Any]:
    """把同词（lexicon+term）的多行聚合为一行，别名汇总展示。"""
    first = rows[0]
    return {
        "lexicon": first.lexicon,
        "term": first.term,
        "canonical": first.canonical,
        "functions": _loads(first.functions_json, []),
        "unit": first.unit,
        "numeric_value": first.numeric_value,
        "source": first.source,
        "entry_type": first.entry_type,
        "aliases": [row.alias for row in rows],
        "entry_ids": [row.id for row in rows],
        "enabled": any(row.enabled for row in rows),
        # 任一子行是系统词条，则该组按系统对待（名称等字段锁定）
        "system": any(row.entry_type != 2 for row in rows),
        "updated_at": max(
            (r.updated_at for r in rows if r.updated_at), default=None
        ).isoformat()
        if any(r.updated_at for r in rows)
        else None,
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

    # 以 (lexicon, term) 作为词条分组键，先按组分页取顶层词
    group_cols = (VoiceLexiconEntry.lexicon, VoiceLexiconEntry.term)
    group_base = (
        select(*group_cols)
        .select_from(VoiceLexiconEntry)
    )
    if conditions:
        group_base = group_base.where(*conditions)
    total = (
        await session.execute(select(func.count()).select_from(group_base.subquery()))
    ).scalar_one()
    groups = (
        await session.execute(
            group_base.group_by(*group_cols)
            .order_by(
                VoiceLexiconEntry.lexicon.asc(),
                VoiceLexiconEntry.term.asc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items: list[dict[str, Any]] = []
    if groups:
        # 取这些组的所有行再做 Python 聚合（每词别名数量有限，无损分页）
        rows = (
            await session.scalars(
                select(VoiceLexiconEntry)
                .where(
                    VoiceLexiconEntry.lexicon.in_({g[0] for g in groups}),
                    VoiceLexiconEntry.term.in_({g[1] for g in groups}),
                )
                .order_by(VoiceLexiconEntry.lexicon, VoiceLexiconEntry.term, VoiceLexiconEntry.id)
            )
        ).all()
        bucket: dict[tuple[str, str], list[VoiceLexiconEntry]] = {}
        for row in rows:
            bucket.setdefault((row.lexicon, row.term), []).append(row)
        # 只取本页请求的组键，避免独立 in_ 兜底误带出其它 (lexicon, term) 组合
        keys = [(g[0], g[1]) for g in groups]
        items = [_group_out(bucket.get(key, [])) for key in keys]
        items = sorted(
            items,
            key=lambda it: (
                it["lexicon"],
                it["term"],
            ),
        )
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.post("/entries", status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: EntryIn, admin: AdminUser, session: DbSession, request: Request
) -> dict[str, Any]:
    _check_lexicon(payload.lexicon)
    aliases = [a.strip() for a in (payload.aliases or []) if a.strip()]
    if payload.alias:
        aliases.insert(0, payload.alias.strip())
    if payload.alias and not payload.term:
        # 兼容旧前端：不传 term 时用第一条别名兜底
        payload.term = payload.alias
    aliases = list(dict.fromkeys(aliases))
    if not aliases:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "at least one alias required"
        )
    add = []
    for alias in aliases:
        row = VoiceLexiconEntry(
            lexicon=payload.lexicon,
            term=payload.term or alias,
            canonical=payload.canonical,
            alias=alias,
            functions_json=json.dumps(payload.functions, ensure_ascii=False),
            unit=payload.unit,
            numeric_value=payload.numeric_value,
            entry_type=2,
            source="manual",
            enabled=True,
        )
        add.append(row)
    session.add_all(add)
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
        target=f"{payload.lexicon}:{payload.term}"[:255],
        detail={"aliases": aliases, "entry_count": len(add)},
    )
    await session.commit()
    await _reload_engine(request)
    return _group_out(add)


async def _group_rows(session, lexicon: str, term: str) -> list[VoiceLexiconEntry]:
    result = await session.scalars(
        select(VoiceLexiconEntry).where(
            VoiceLexiconEntry.lexicon == lexicon,
            VoiceLexiconEntry.term == term,
        )
    )
    rows = list(result.all())
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "entry group not found")
    return rows


@router.put("/entries/group")
async def update_entry_group(
    payload: EntryGroupIn, admin: AdminUser, session: DbSession, request: Request
) -> dict[str, Any]:
    _check_lexicon(payload.lexicon)
    rows = await _group_rows(session, payload.lexicon, payload.term)
    system = any(r.entry_type != 2 for r in rows)

    # 收集要改写的字段
    changes = payload.model_dump(exclude_unset=True)
    new_term = changes.pop("new_term", None)
    aliases = changes.pop("aliases", None)
    set_fields = {}
    for key in ("canonical", "functions", "unit", "numeric_value"):
        if key in changes and changes[key] is not None:
            set_fields[key] = changes[key]

    def _set_common(row: VoiceLexiconEntry) -> None:
        for key, value in set_fields.items():
            if key == "functions":
                row.functions_json = json.dumps(value, ensure_ascii=False)
            else:
                setattr(row, key, value)
        if new_term is not None and new_term != row.term:
            row.term = new_term
        row.updated_at = utcnow()

    existing_ids = [r.id for r in rows]
    if system:
        # 系统组只能整体启停
        if new_term is not None or set_fields or aliases is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "system entries only allow enabling or disabling",
            )
        if changes.get("enabled") is not None:
            for r in rows:
                r.enabled = bool(changes["enabled"])
                r.updated_at = utcnow()
        add_audit(
            session,
            member_id=admin.id,
            action="fastpath.voice.entry_updated",
            target=f"{payload.lexicon}:{payload.term}"[:255],
            detail={"group": True, "entry_ids": existing_ids, "changes": changes},
        )
        await session.commit()
        await _reload_engine(request)
        fresh = await _group_rows(session, payload.lexicon, new_term or payload.term)
        return _group_out(fresh)

    if aliases is not None:
        aliases = [a.strip() for a in aliases if a.strip()]
        aliases = list(dict.fromkeys(aliases))
        if not aliases:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "at least one alias required"
            )
        # 别名集合整体替换：删除多余行，更新/新增其余
        existing_by_alias: dict[str, VoiceLexiconEntry] = {}
        for r in rows:
            existing_by_alias[r.alias] = r
        target_aliases = list(aliases)
        add = []
        for alias in target_aliases:
            row = existing_by_alias.pop(alias, None)
            if row is None:
                row = VoiceLexiconEntry(
                    lexicon=payload.lexicon,
                    term=new_term or payload.term,
                    canonical=set_fields.get("canonical", rows[0].canonical),
                    alias=alias,
                    functions_json=rows[0].functions_json,
                    unit=rows[0].unit,
                    numeric_value=rows[0].numeric_value,
                    entry_type=2,
                    source="manual",
                    enabled=True,
                )
                add.append(row)
            else:
                _set_common(row)
        # 删除被移除的别名行
        orphan_ids = [r.id for r in existing_by_alias.values()]
        if orphan_ids:
            await session.execute(
                VoiceLexiconEntry.__table__.delete().where(
                    VoiceLexiconEntry.id.in_(orphan_ids)
                )
            )
        session.add_all(add)
        session.flush()
    else:
        for r in rows:
            _set_common(r)
        if changes.get("enabled") is not None:
            enabled = bool(changes["enabled"])
            for r in rows:
                r.enabled = enabled

    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.entry_updated",
        target=f"{payload.lexicon}:{payload.term}"[:255],
        detail={"group": True, "entry_ids": existing_ids, "changes": changes},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "alias already exists in this lexicon"
        ) from exc
    await _reload_engine(request)
    fresh = await _group_rows(session, payload.lexicon, new_term or payload.term)
    return _group_out(fresh)


@router.delete("/entries/group", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry_group(
    payload: EntryGroupDeleteIn, admin: AdminUser, session: DbSession, request: Request
) -> None:
    _check_lexicon(payload.lexicon)
    rows = await _group_rows(session, payload.lexicon, payload.term)
    if any(r.entry_type != 2 for r in rows):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "system entries cannot be deleted",
        )
    add_audit(
        session,
        member_id=admin.id,
        action="fastpath.voice.entry_deleted",
        target=f"{payload.lexicon}:{payload.term}"[:255],
        detail={"entry_ids": [r.id for r in rows]},
    )
    await session.execute(
        VoiceLexiconEntry.__table__.delete().where(
            VoiceLexiconEntry.id.in_([r.id for r in rows])
        )
    )
    await session.commit()
    await _reload_engine(request)

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


def _group_list_out(row: VoiceGroupList) -> dict[str, Any]:
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
    return {"items": [_group_list_out(row) for row in rows]}


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
        detail={"group_list": _group_list_out(row)},
    )
    await session.commit()
    await _reload_engine(request)
    return _group_list_out(row)


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
        detail={"group_list": _group_list_out(row)},
    )
    await session.commit()
    await _reload_engine(request)
    return _group_list_out(row)


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
