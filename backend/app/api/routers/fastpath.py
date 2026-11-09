from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.deps import AdminUser, ConfigDep, CurrentUser, DbSession
from app.db.models import EntityAlias, IntentRule, IntentRuleVersion
from app.fastpath.rules import validate_phrases, validate_rule
from app.services.audit import add_audit

router = APIRouter(prefix="/api/fastpath", tags=["fastpath"])


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(default="scene", max_length=16)
    enabled: bool = True
    priority: int = 100
    phrases: list[str] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    reply_template: str = Field(default="", max_length=255)


class RollbackIn(BaseModel):
    version: int


class AliasIn(BaseModel):
    entity_id: str = Field(min_length=3, max_length=128)
    alias: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=64)
    area_id: str | None = Field(default=None, max_length=64)
    domain: str = Field(default="", max_length=32)
    can_control: bool = True


class TestIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _load_json(raw: str | None, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _rule_out(row: IntentRule) -> dict[str, Any]:
    match = _load_json(row.match_json, {})
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "enabled": row.enabled,
        "priority": row.priority,
        "phrases": match.get("phrases") or [],
        "actions": _load_json(row.action_json, []),
        "reply_template": row.reply_template,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _alias_out(row: EntityAlias) -> dict[str, Any]:
    return {
        "id": row.id,
        "entity_id": row.entity_id,
        "alias": row.alias,
        "display_name": row.display_name,
        "area_id": row.area_id,
        "domain": row.domain,
        "can_control": row.can_control,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _snapshot(row: IntentRule) -> dict[str, Any]:
    return {
        "name": row.name,
        "kind": row.kind,
        "enabled": row.enabled,
        "priority": row.priority,
        "match_json": row.match_json,
        "action_json": row.action_json,
        "reply_template": row.reply_template,
    }


async def _reload_engine(request: Request) -> None:
    engine = getattr(request.app.state, "fast_path", None)
    if engine is not None:
        await engine.reload()


def _validate(config, payload: RuleIn) -> dict[str, Any]:
    phrase_error = validate_phrases(payload.phrases)
    if phrase_error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, phrase_error)
    action_error = validate_rule(payload.actions, config.whitelist())
    if action_error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, action_error)
    if payload.kind != "scene":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "only scene rules are supported")
    return {
        "match_json": json.dumps({"phrases": payload.phrases}, ensure_ascii=False),
        "action_json": json.dumps(payload.actions, ensure_ascii=False),
    }


@router.get("/rules")
async def list_rules(
    _: AdminUser,
    session: DbSession,
    q: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    stmt = select(IntentRule).order_by(
        IntentRule.priority.desc(), IntentRule.id.desc()
    )
    if q:
        stmt = stmt.where(IntentRule.name.contains(q))
    rows = (await session.scalars(stmt)).all()
    return {"items": [_rule_out(row) for row in rows], "total": len(rows)}


@router.post("/rules")
async def create_rule(
    payload: RuleIn,
    user: AdminUser,
    session: DbSession,
    config: ConfigDep,
    request: Request,
) -> dict[str, Any]:
    if await session.scalar(select(IntentRule).where(IntentRule.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "rule name already exists")
    fields = _validate(config, payload)
    row = IntentRule(
        name=payload.name,
        kind="scene",
        enabled=payload.enabled,
        priority=payload.priority,
        reply_template=payload.reply_template,
        version=1,
        created_by=user.id,
        **fields,
    )
    session.add(row)
    await session.flush()
    session.add(
        IntentRuleVersion(
            rule_id=row.id,
            version=1,
            snapshot_json=json.dumps(_snapshot(row), ensure_ascii=False),
            changed_by=user.id,
        )
    )
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.rule_created",
        target=str(row.id),
        detail={"name": row.name, "priority": row.priority},
    )
    await session.commit()
    await session.refresh(row)
    await _reload_engine(request)
    return _rule_out(row)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    payload: RuleIn,
    user: AdminUser,
    session: DbSession,
    config: ConfigDep,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(IntentRule, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    if payload.name != row.name and await session.scalar(
        select(IntentRule).where(IntentRule.name == payload.name)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "rule name already exists")
    fields = _validate(config, payload)
    row.name = payload.name
    row.enabled = payload.enabled
    row.priority = payload.priority
    row.reply_template = payload.reply_template
    row.match_json = fields["match_json"]
    row.action_json = fields["action_json"]
    row.version += 1
    session.add(
        IntentRuleVersion(
            rule_id=row.id,
            version=row.version,
            snapshot_json=json.dumps(_snapshot(row), ensure_ascii=False),
            changed_by=user.id,
        )
    )
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.rule_updated",
        target=str(row.id),
        detail={"name": row.name, "version": row.version},
    )
    await session.commit()
    await session.refresh(row)
    await _reload_engine(request)
    return _rule_out(row)


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    user: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(IntentRule, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    await session.execute(
        delete(IntentRuleVersion).where(IntentRuleVersion.rule_id == rule_id)
    )
    await session.delete(row)
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.rule_deleted",
        target=str(rule_id),
        detail={"name": row.name},
    )
    await session.commit()
    await _reload_engine(request)
    return {"deleted": True, "id": rule_id}


@router.get("/rules/{rule_id}/versions")
async def list_versions(rule_id: int, _: AdminUser, session: DbSession) -> dict[str, Any]:
    if await session.get(IntentRule, rule_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    rows = (
        await session.scalars(
            select(IntentRuleVersion)
            .where(IntentRuleVersion.rule_id == rule_id)
            .order_by(IntentRuleVersion.version.desc())
        )
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "version": row.version,
                "snapshot": _load_json(row.snapshot_json, {}),
                "changed_by": row.changed_by,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    }


@router.post("/rules/{rule_id}/rollback")
async def rollback_rule(
    rule_id: int,
    payload: RollbackIn,
    user: AdminUser,
    session: DbSession,
    config: ConfigDep,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(IntentRule, rule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    snapshot_row = await session.scalar(
        select(IntentRuleVersion).where(
            IntentRuleVersion.rule_id == rule_id,
            IntentRuleVersion.version == payload.version,
        )
    )
    if snapshot_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule version not found")
    snapshot = _load_json(snapshot_row.snapshot_json, {})
    phrases = _load_json(snapshot.get("match_json"), {}).get("phrases") or []
    actions = _load_json(snapshot.get("action_json"), [])
    check = RuleIn(
        name=str(snapshot.get("name") or row.name),
        kind="scene",
        enabled=bool(snapshot.get("enabled", True)),
        priority=int(snapshot.get("priority", 100)),
        phrases=phrases,
        actions=actions,
        reply_template=str(snapshot.get("reply_template") or ""),
    )
    fields = _validate(config, check)
    row.name = check.name
    row.enabled = check.enabled
    row.priority = check.priority
    row.reply_template = check.reply_template
    row.match_json = fields["match_json"]
    row.action_json = fields["action_json"]
    row.version += 1
    session.add(
        IntentRuleVersion(
            rule_id=row.id,
            version=row.version,
            snapshot_json=json.dumps(_snapshot(row), ensure_ascii=False),
            changed_by=user.id,
        )
    )
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.rule_rolled_back",
        target=str(row.id),
        detail={"from_version": payload.version, "to_version": row.version},
    )
    await session.commit()
    await session.refresh(row)
    await _reload_engine(request)
    return _rule_out(row)


@router.get("/aliases")
async def list_aliases(
    _: AdminUser,
    session: DbSession,
    q: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    stmt = select(EntityAlias).order_by(EntityAlias.id.desc())
    if q:
        stmt = stmt.where(
            EntityAlias.alias.contains(q) | EntityAlias.entity_id.contains(q)
        )
    rows = (await session.scalars(stmt)).all()
    return {"items": [_alias_out(row) for row in rows], "total": len(rows)}


@router.post("/aliases")
async def create_alias(
    payload: AliasIn,
    user: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    if "." not in payload.entity_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "entity_id must look like domain.object_id"
        )
    if await session.scalar(select(EntityAlias).where(EntityAlias.alias == payload.alias)):
        raise HTTPException(status.HTTP_409_CONFLICT, "alias already exists")
    row = EntityAlias(
        entity_id=payload.entity_id,
        alias=payload.alias.strip(),
        display_name=payload.display_name.strip(),
        area_id=(payload.area_id or "").strip() or None,
        domain=payload.domain.strip() or payload.entity_id.split(".", 1)[0],
        can_control=payload.can_control,
    )
    session.add(row)
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.alias_created",
        target=row.entity_id,
        detail={"alias": row.alias},
    )
    await session.commit()
    await session.refresh(row)
    await _reload_engine(request)
    return _alias_out(row)


@router.put("/aliases/{alias_id}")
async def update_alias(
    alias_id: int,
    payload: AliasIn,
    user: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(EntityAlias, alias_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alias not found")
    duplicate = await session.scalar(
        select(EntityAlias).where(
            EntityAlias.alias == payload.alias, EntityAlias.id != alias_id
        )
    )
    if duplicate:
        raise HTTPException(status.HTTP_409_CONFLICT, "alias already exists")
    if "." not in payload.entity_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "entity_id must look like domain.object_id"
        )
    row.entity_id = payload.entity_id
    row.alias = payload.alias.strip()
    row.display_name = payload.display_name.strip()
    row.area_id = (payload.area_id or "").strip() or None
    row.domain = payload.domain.strip() or payload.entity_id.split(".", 1)[0]
    row.can_control = payload.can_control
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.alias_updated",
        target=row.entity_id,
        detail={"alias": row.alias},
    )
    await session.commit()
    await session.refresh(row)
    await _reload_engine(request)
    return _alias_out(row)


@router.delete("/aliases/{alias_id}")
async def delete_alias(
    alias_id: int,
    user: AdminUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    row = await session.get(EntityAlias, alias_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alias not found")
    await session.delete(row)
    add_audit(
        session,
        member_id=user.id,
        action="fastpath.alias_deleted",
        target=row.entity_id,
        detail={"alias": row.alias},
    )
    await session.commit()
    await _reload_engine(request)
    return {"deleted": True, "id": alias_id}


@router.post("/test")
async def test_text(
    payload: TestIn, user: CurrentUser, request: Request
) -> dict[str, Any]:
    engine = getattr(request.app.state, "fast_path", None)
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "fast path is not available")
    return await engine.test(payload.text, user)
