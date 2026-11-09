from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminUser, DbSession
from app.db.models import ChannelIdentity, Member, utcnow
from app.services.audit import add_audit

router = APIRouter(prefix="/api/channels", tags=["channels"])

# 渠道账号 ID 的前缀特征，用来挡住「把应用 ID 当账号 ID」这类错误。
# identity.channel 存的是实例名（feishu / feishu_2 …），按 feishu 前缀归一。
_ID_PREFIX = {"feishu": ("ou_", "on_")}
_ID_HINT = {
    "feishu": "飞书渠道账号要填成员的 open_id（形如 ou_xxxxxxxx），不是应用的 App ID（cli_ 开头）。",
}


def _feishu_base(channel: str) -> str | None:
    """feishu_2 → feishu；非飞书渠道返回 None。"""
    base = channel.rsplit("_", 1)[0] if channel.startswith("feishu_") else channel
    return base if base == "feishu" else None


def _check_external_id(channel: str, external_id: str) -> None:
    base = _feishu_base(channel)
    prefixes = _ID_PREFIX.get(base) if base else None
    if not prefixes:
        return
    if external_id.startswith(prefixes):
        return
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _ID_HINT[base])


class FeishuInstanceIn(BaseModel):
    # 实例名跟随行保存（feishu / feishu_2 …），省略时由后端按位置或旧行补
    name: str = Field(default="", max_length=16)
    enabled: bool = False
    app_id: str = Field(default="", max_length=128)
    # 留空表示沿用已保存的密钥；只有显式传入才会覆盖
    app_secret: str | None = Field(default=None, max_length=256)
    push_enabled: bool = True
    remark: str = Field(default="", max_length=64)


class FeishuConfigIn(BaseModel):
    instances: list[FeishuInstanceIn] = Field(default_factory=list, max_length=10)


class AsrConfigIn(BaseModel):
    # 语音识别全局配置；api_key 留空表示沿用已保存的密钥
    enabled: bool = False
    api_base: str = Field(default="", max_length=256)
    api_key: str | None = Field(default=None, max_length=256)
    model: str = Field(default="", max_length=128)
    language: str = Field(default="zh", max_length=16)
    timeout_seconds: int = Field(default=30, ge=5, le=120)


class IdentityIn(BaseModel):
    channel: str = Field(min_length=1, max_length=16)
    external_id: str = Field(min_length=1, max_length=128)
    member_id: int
    display_name: str = Field(default="", max_length=64)
    chat_id: str = Field(default="", max_length=128)
    enabled: bool = True


class IdentityPatch(BaseModel):
    member_id: int | None = None
    display_name: str | None = Field(default=None, max_length=64)
    chat_id: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None


def _manager(request: Request):
    manager = getattr(request.app.state, "channel_manager", None)
    if manager is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "channel service is not ready"
        )
    return manager


def _serialize_identity(identity: ChannelIdentity, member: Member | None) -> dict[str, Any]:
    try:
        meta = json.loads(identity.meta_json or "{}")
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": identity.id,
        "channel": identity.channel,
        "external_id": identity.external_id,
        "member_id": identity.member_id,
        "member_name": (member.display_name or member.username) if member else "",
        "display_name": identity.display_name,
        "chat_id": str(meta.get("chat_id") or ""),
        "enabled": identity.enabled,
        "created_at": identity.created_at.isoformat() if identity.created_at else None,
        "updated_at": identity.updated_at.isoformat() if identity.updated_at else None,
    }


@router.get("")
async def get_overview(_: AdminUser, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    return manager.status()


@router.put("/feishu")
async def save_feishu(
    payload: FeishuConfigIn,
    user: AdminUser,
    request: Request,
    session: DbSession,
) -> dict[str, Any]:
    manager = _manager(request)
    for item in payload.instances:
        if item.enabled and not item.app_id.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "App ID 不能为空")
    # App ID 必须互不相同，否则两条长连接会抢同一批事件
    app_ids = [item.app_id.strip() for item in payload.instances if item.app_id.strip()]
    if len(app_ids) != len(set(app_ids)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "App ID 不能重复")
    # 实例名也不能重复，dispatcher 注册表按名字索引
    names = [item.name.strip() for item in payload.instances if item.name.strip()]
    if len(names) != len(set(names)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "应用实例名不能重复")
    views = await manager.save_feishu_instances(
        [item.model_dump() for item in payload.instances]
    )
    add_audit(
        session,
        member_id=user.id,
        action="channel.feishu.save",
        target="feishu",
        detail={
            "count": len(views),
            "enabled": [v["app_id"] for v in views if v["enabled"]],
            "push_enabled_any": any(v["push_enabled"] for v in views),
        },
    )
    await session.commit()
    await manager.apply()
    return manager.status()


@router.post("/feishu/reload")
async def reload_channels(user: AdminUser, request: Request, session: DbSession) -> dict[str, Any]:
    manager = _manager(request)
    add_audit(
        session, member_id=user.id, action="channel.reload", target="all", detail={}
    )
    await session.commit()
    await manager.apply()
    return manager.status()


@router.put("/asr")
async def save_asr(
    payload: AsrConfigIn, user: AdminUser, request: Request, session: DbSession
) -> dict[str, Any]:
    """保存语音识别配置并热重建适配器（语音开关即时生效）。"""
    from app.services.asr import DEFAULT_ASR

    manager = _manager(request)
    current = manager.asr_config()
    api_key = payload.api_key
    if api_key is None or not api_key.strip():
        api_key = str(current.get("api_key") or "")  # 留空沿用旧密钥
    config = dict(DEFAULT_ASR)
    config.update(current)
    config.update(
        {
            "enabled": payload.enabled,
            "api_base": payload.api_base.strip(),
            "api_key": api_key.strip(),
            # 模型 / 语言留空时沿用已保存的值，避免误存把配置打回默认
            "model": payload.model.strip()
            or str(current.get("model") or "")
            or str(DEFAULT_ASR["model"]),
            "language": payload.language.strip()
            or str(current.get("language") or "")
            or str(DEFAULT_ASR["language"]),
            "timeout_seconds": payload.timeout_seconds,
        }
    )
    if config["enabled"] and not config["api_base"].startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "启用语音识别必须填写 API 地址（http/https 开头）",
        )
    await manager.save_asr_config(config)
    add_audit(
        session,
        member_id=user.id,
        action="channel.asr.save",
        target="asr",
        detail={"enabled": config["enabled"], "api_base": config["api_base"], "model": config["model"]},
    )
    await session.commit()
    await manager.apply()
    return manager.status()


@router.get("/identities")
async def list_identities(_: AdminUser, session: DbSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ChannelIdentity).order_by(ChannelIdentity.channel, ChannelIdentity.id)
        )
    ).scalars().all()
    members = {
        member.id: member
        for member in (await session.scalars(select(Member))).all()
    }
    return [_serialize_identity(row, members.get(row.member_id)) for row in rows]


@router.post("/identities", status_code=status.HTTP_201_CREATED)
async def create_identity(
    payload: IdentityIn, user: AdminUser, request: Request, session: DbSession
) -> dict[str, Any]:
    base = _feishu_base(payload.channel)
    if base is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"暂不支持的渠道：{payload.channel}"
        )
    _check_external_id(payload.channel, payload.external_id.strip())
    manager = _manager(request)
    # 只允许绑定到当前已配置的飞书应用实例上，避免存进一个永远收不到消息的渠道名
    active = {item["name"] for item in manager.status()["feishu"]["instances"]}
    if payload.channel not in active:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"未配置的飞书应用：{payload.channel}"
        )
    member = await session.get(Member, payload.member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    meta = {"chat_id": payload.chat_id} if payload.chat_id else {}
    identity = ChannelIdentity(
        channel=payload.channel,
        external_id=payload.external_id.strip(),
        member_id=payload.member_id,
        display_name=payload.display_name,
        meta_json=json.dumps(meta, ensure_ascii=False),
        enabled=payload.enabled,
    )
    session.add(identity)
    add_audit(
        session,
        member_id=user.id,
        action="channel.identity.create",
        target=payload.channel,
        detail={
            "external_id": payload.external_id,
            "member_id": payload.member_id,
        },
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "该渠道账号已绑定") from None
    await session.refresh(identity)
    # 绑定完成后把「待绑定」里的这条清掉
    manager.dispatcher.forget_pending(payload.channel, identity.external_id)
    return _serialize_identity(identity, member)


@router.patch("/identities/{identity_id}")
async def update_identity(
    identity_id: int, payload: IdentityPatch, user: AdminUser, session: DbSession
) -> dict[str, Any]:
    identity = await session.get(ChannelIdentity, identity_id)
    if identity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "identity not found")
    if payload.member_id is not None:
        member = await session.get(Member, payload.member_id)
        if member is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
        identity.member_id = payload.member_id
    if payload.display_name is not None:
        identity.display_name = payload.display_name
    if payload.enabled is not None:
        identity.enabled = payload.enabled
    if payload.chat_id is not None:
        try:
            meta = json.loads(identity.meta_json or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["chat_id"] = payload.chat_id
        identity.meta_json = json.dumps(meta, ensure_ascii=False)
    identity.updated_at = utcnow()
    add_audit(
        session,
        member_id=user.id,
        action="channel.identity.update",
        target=f"{identity.channel}:{identity.external_id}",
        detail=payload.model_dump(exclude_none=True),
    )
    await session.commit()
    await session.refresh(identity)
    member = await session.get(Member, identity.member_id)
    return _serialize_identity(identity, member)


@router.delete("/identities/{identity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity(identity_id: int, user: AdminUser, session: DbSession) -> None:
    identity = await session.get(ChannelIdentity, identity_id)
    if identity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "identity not found")
    add_audit(
        session,
        member_id=user.id,
        action="channel.identity.delete",
        target=f"{identity.channel}:{identity.external_id}",
        detail={"member_id": identity.member_id},
    )
    await session.delete(identity)
    await session.commit()
