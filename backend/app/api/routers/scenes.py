from __future__ import annotations

import uuid
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, ConfigDep, DbSession, HADep
from app.chat.automation import AutomationValidationError, parse_automation_admin
from app.ha.adapter import HAAdapter
from app.ha.client_rest import HAClientError
from app.services.audit import add_audit

router = APIRouter(prefix="/api/scenes", tags=["scenes"])

SAFE_SCENE_ID = r"^[A-Za-z0-9_-]{1,64}$"


class AutomationIn(BaseModel):
    yaml: str = Field(min_length=1, max_length=40000)
    config_id: str = Field(default="", max_length=64)


class HaSceneIn(BaseModel):
    scene_id: str = Field(min_length=1, max_length=64, pattern=SAFE_SCENE_ID)
    snapshot_entities: list[str] = Field(default_factory=list)
    entities: dict[str, Any] | None = None


def _rest(adapter: HAAdapter):
    if adapter.rest is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "尚未连接到 Home Assistant"
        )
    return adapter.rest


async def _ha_states(adapter: HAAdapter) -> list[dict[str, Any]]:
    try:
        return await _rest(adapter).get_states()
    except HAClientError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


def _entity_brief(state: dict[str, Any]) -> dict[str, Any]:
    attributes = state.get("attributes") or {}
    return {
        "entity_id": state.get("entity_id", ""),
        "name": attributes.get("friendly_name") or state.get("entity_id", ""),
        "state": state.get("state"),
        "last_changed": state.get("last_changed"),
        "last_triggered": attributes.get("last_triggered"),
        "mode": attributes.get("mode"),
        "entities": attributes.get("entity_id") or [],
    }


def _scenes_from(states: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
    return [
        _entity_brief(state)
        for state in states
        if str(state.get("entity_id", "")).startswith(f"{domain}.")
    ]


def _automation_brief(state: dict[str, Any]) -> dict[str, Any]:
    brief = _entity_brief(state)
    attributes = state.get("attributes") or {}
    brief["config_id"] = str(attributes.get("id") or brief["entity_id"].split(".", 1)[-1])
    brief.pop("entities", None)
    return brief


@router.get("/overview")
async def overview(_: AdminUser, adapter: HADep) -> dict[str, Any]:
    """HA 场景、脚本与自动化的总览，列表直接读 HA，不受白名单过滤影响。"""
    states = await _ha_states(adapter)
    return {
        "ha_scenes": _scenes_from(states, "scene"),
        "scripts": _scenes_from(states, "script"),
        "automations": [
            _automation_brief(state)
            for state in states
            if str(state.get("entity_id", "")).startswith("automation.")
        ],
    }


@router.get("/automations/{config_id}")
async def read_automation(
    config_id: str, _: AdminUser, adapter: HADep
) -> dict[str, Any]:
    try:
        config = await _rest(adapter).get_automation_config(config_id)
    except HAClientError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "自动化不存在")
    return {"config_id": config_id, "config": config, "yaml": _dump_yaml(config)}


def _dump_yaml(config: dict[str, Any]) -> str:
    try:
        return yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    except yaml.YAMLError:
        return ""


@router.post("/automations")
async def create_automation(
    payload: AutomationIn,
    user: AdminUser,
    session: DbSession,
    config: ConfigDep,
    adapter: HADep,
) -> dict[str, Any]:
    try:
        parsed = parse_automation_admin(payload.yaml, config.whitelist())
    except AutomationValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    config_id = (payload.config_id or parsed.get("id") or "").strip()
    if not config_id:
        config_id = f"ha_agent_{uuid.uuid4().hex[:12]}"
    parsed["id"] = config_id
    try:
        await _rest(adapter).write_automation(config_id, parsed)
    except HAClientError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    add_audit(
        session,
        member_id=user.id,
        action="scene.automation_created",
        target=config_id,
        detail={"alias": parsed.get("alias", "")},
    )
    await session.commit()
    return {"config_id": config_id, "config": parsed, "yaml": _dump_yaml(parsed)}


@router.put("/automations/{config_id}")
async def update_automation(
    config_id: str,
    payload: AutomationIn,
    user: AdminUser,
    session: DbSession,
    config: ConfigDep,
    adapter: HADep,
) -> dict[str, Any]:
    try:
        parsed = parse_automation_admin(payload.yaml, config.whitelist())
    except AutomationValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    parsed["id"] = config_id
    try:
        await _rest(adapter).write_automation(config_id, parsed)
    except HAClientError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    add_audit(
        session,
        member_id=user.id,
        action="scene.automation_updated",
        target=config_id,
        detail={"alias": parsed.get("alias", "")},
    )
    await session.commit()
    return {"config_id": config_id, "config": parsed, "yaml": _dump_yaml(parsed)}


@router.delete("/automations/{config_id}")
async def delete_automation(
    config_id: str,
    user: AdminUser,
    session: DbSession,
    adapter: HADep,
) -> dict[str, Any]:
    try:
        await _rest(adapter).delete_automation(config_id)
    except HAClientError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    add_audit(
        session,
        member_id=user.id,
        action="scene.automation_deleted",
        target=config_id,
    )
    await session.commit()
    return {"deleted": True, "config_id": config_id}


@router.post("/automations/{entity_id}/trigger")
async def trigger_automation(
    entity_id: str, user: AdminUser, session: DbSession, adapter: HADep
) -> dict[str, Any]:
    await _call(adapter, "automation", "trigger", {"entity_id": entity_id})
    add_audit(
        session,
        member_id=user.id,
        action="scene.automation_triggered",
        target=entity_id,
    )
    await session.commit()
    return {"triggered": True, "entity_id": entity_id}


@router.post("/ha")
async def create_ha_scene(
    payload: HaSceneIn,
    user: AdminUser,
    session: DbSession,
    adapter: HADep,
) -> dict[str, Any]:
    data: dict[str, Any] = {"scene_id": payload.scene_id}
    if payload.snapshot_entities:
        data["snapshot_entities"] = payload.snapshot_entities
    if payload.entities:
        data["entities"] = payload.entities
    await _call(adapter, "scene", "create", data)
    await _call(adapter, "scene", "reload", {})
    add_audit(
        session,
        member_id=user.id,
        action="scene.ha_scene_created",
        target=payload.scene_id,
        detail={"snapshot_entities": payload.snapshot_entities},
    )
    await session.commit()
    return {"created": True, "scene_id": payload.scene_id}


@router.delete("/ha/{entity_id}")
async def delete_ha_scene(
    entity_id: str, user: AdminUser, session: DbSession, adapter: HADep
) -> dict[str, Any]:
    # HA scene.delete 没有 scene_id 字段，只能通过 target.entity 定位
    # （REST 调用以 entity_id 键指定），必须保留完整实体 id。
    await _call(adapter, "scene", "delete", {"entity_id": entity_id})
    await _call(adapter, "scene", "reload", {})
    add_audit(
        session,
        member_id=user.id,
        action="scene.ha_scene_deleted",
        target=entity_id,
    )
    await session.commit()
    return {"deleted": True, "entity_id": entity_id}


@router.post("/ha/{entity_id}/activate")
async def activate_ha_scene(
    entity_id: str, user: AdminUser, session: DbSession, adapter: HADep
) -> dict[str, Any]:
    await _call(adapter, "scene", "turn_on", {"entity_id": entity_id})
    add_audit(
        session,
        member_id=user.id,
        action="scene.ha_scene_activated",
        target=entity_id,
    )
    await session.commit()
    return {"activated": True, "entity_id": entity_id}


@router.post("/scripts/{entity_id}/activate")
async def activate_script(
    entity_id: str, user: AdminUser, session: DbSession, adapter: HADep
) -> dict[str, Any]:
    await _call(adapter, "script", "turn_on", {"entity_id": entity_id})
    add_audit(
        session,
        member_id=user.id,
        action="scene.script_activated",
        target=entity_id,
    )
    await session.commit()
    return {"activated": True, "entity_id": entity_id}


async def _call(
    adapter: HAAdapter, domain: str, service: str, data: dict[str, Any]
) -> None:
    try:
        await adapter.call_service(domain, service, data)
    except HAClientError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
