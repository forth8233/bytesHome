from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog

from app.channels.dispatcher import ChannelDispatcher
from app.channels.feishu import FeishuAdapter

logger = structlog.get_logger(__name__)

FEISHU_KEY = "channels.feishu"
ASR_KEY = "channels.asr"

# 实例名规则：feishu / feishu_2 / feishu_3 …（第一个应用沿用旧名，兼容存量绑定数据）
FEISHU_NAME = re.compile(r"feishu(?:_\d+)?")


def feishu_instances(raw: Any) -> list[dict[str, Any]]:
    """channels.feishu 的存量是单应用 dict，新格式是实例列表，这里统一成列表。"""
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


class ChannelManager:
    """按配置装配适配器，并把它们挂在 dispatcher 上。

    配置改动（/api/channels）后调用 apply() 热重启，不需要重启进程。
    """

    def __init__(self, *, config, session_factory, chat_service, proposals) -> None:
        self._config = config
        self._session_factory = session_factory
        self._chat = chat_service
        self._proposals = proposals
        self.dispatcher = ChannelDispatcher(
            session_factory=session_factory,
            chat_service=chat_service,
            proposals=proposals,
            config=config,
        )
        self._tasks: list[asyncio.Task] = []
        self._generation = 0

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        await self.dispatcher.start()
        await self.apply()

    async def stop(self) -> None:
        await self._stop_adapters()
        await self.dispatcher.stop()

    async def apply(self) -> dict[str, Any]:
        """按当前配置重建适配器（先停旧的，再起新的）。"""
        self._generation += 1
        generation = self._generation
        await self._stop_adapters()
        adapters: list = []
        transcriber = self._build_transcriber()
        for index, cfg in enumerate(feishu_instances(self._config.get(FEISHU_KEY)), start=1):
            if not cfg.get("enabled") or not str(cfg.get("app_id") or "").strip():
                continue
            adapters.append(
                FeishuAdapter(
                    name=_instance_name(cfg, index),
                    app_id=str(cfg.get("app_id") or ""),
                    app_secret=str(cfg.get("app_secret") or ""),
                    dispatcher=self.dispatcher,
                    transcriber=transcriber,
                )
            )

        for adapter in adapters:
            self.dispatcher.register(adapter)
            self._tasks.append(
                asyncio.create_task(
                    self._start_adapter(adapter, generation),
                    name=f"channel-start-{adapter.name}-{generation}",
                )
            )
        logger.info(
            "channel.manager.apply",
            generation=generation,
            adapters=[a.name for a in adapters],
        )
        return self.status()

    async def _start_adapter(self, adapter, generation: int) -> None:
        try:
            await adapter.start()
        except Exception:  # noqa: BLE001 - 单渠道启动失败不影响其它渠道
            logger.exception("channel.start_failed", channel=adapter.name)
            return
        if generation != self._generation:  # 启动期间配置又变了
            await adapter.stop()
            return
        logger.info("channel.started", channel=adapter.name, status=adapter.status())

    async def _stop_adapters(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        adapters = self.dispatcher.adapters()
        for adapter in adapters.values():
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                logger.exception("channel.stop_failed", channel=adapter.name)
        self.dispatcher.unregister_all()

    # ---------- 状态 ----------

    def _build_transcriber(self):
        """按 channels.asr 配置装配转写器；未启用返回 None（语音降级为提示）。"""
        from app.services.asr import build_transcriber

        try:
            return build_transcriber(self._config.get(ASR_KEY))
        except Exception:  # noqa: BLE001 - 配置坏不能拖垮渠道装配
            logger.exception("channel.asr.build_failed")
            return None

    def asr_status(self) -> dict[str, Any]:
        cfg = dict(self._config.get(ASR_KEY) or {})
        cfg.pop("api_key", None)  # 密钥绝不进状态视图
        transcriber = self._build_transcriber()
        instances = {}
        for name, adapter in self.dispatcher.adapters().items():
            if not name.startswith("feishu"):
                continue
            stat = adapter.status()
            instances[name] = {
                "transcribed": stat.get("transcribed", 0),
                "transcribe_failed": stat.get("transcribe_failed", 0),
                "error": stat.get("asr_error"),
            }
        return {"enabled": transcriber is not None, "config": cfg, "instances": instances}

    def status(self) -> dict[str, Any]:
        instances = self._feishu_instances_view()
        return {
            "feishu": {
                "instances": instances,
                "push_enabled_any": any(item["push_enabled"] for item in instances),
            },
            "asr": self.asr_status(),
            "dispatcher": self.dispatcher.stats(),
            "channels": [
                adapter.status() for adapter in self.dispatcher.adapters().values()
            ],
            # 未绑定账号的最近来信，管理员照着 open_id 一键绑定
            "pending": self.dispatcher.pending(),
        }

    def _feishu_instances_view(self) -> list[dict[str, Any]]:
        live = {
            adapter.name
            for adapter in self.dispatcher.adapters().values()
            if adapter.name.startswith("feishu")
        }
        views = []
        for index, cfg in enumerate(feishu_instances(self._config.get(FEISHU_KEY)), start=1):
            name = _instance_name(cfg, index)
            views.append(
                {
                    "name": name,
                    "live": name in live,
                    "enabled": bool(cfg.get("enabled")),
                    "app_id": str(cfg.get("app_id") or ""),
                    "app_secret_set": bool(cfg.get("app_secret")),
                    "push_enabled": bool(cfg.get("push_enabled", True)),
                    "remark": str(cfg.get("remark") or ""),
                }
            )
        return views

    # ---------- 配置写入 ----------

    def asr_config(self) -> dict[str, Any]:
        """channels.asr 的当前配置（含密钥，仅后端内部使用）。"""
        return dict(self._config.get(ASR_KEY) or {})

    async def save_asr_config(self, config: dict[str, Any]) -> None:
        await self._config.set(ASR_KEY, config, encrypted=True)

    async def save_feishu_instances(
        self, instances: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """保存飞书应用列表。

        app_secret 传 None 表示沿用旧值，避免前端回显时误清空；
        实例名按位置固定（第 1 个 = feishu，第 n 个 = feishu_n），
        已绑定到 channel_identity 的名字不因增删应用而错位。
        """
        current = feishu_instances(self._config.get(FEISHU_KEY))
        payload: list[dict[str, Any]] = []
        for index, item in enumerate(instances):
            old = current[index] if index < len(current) else {}
            secret = item.get("app_secret")
            secret = secret.strip() if isinstance(secret, str) else ""
            # 名字跟随行本身：优先沿用提交里的名字，其次旧行的名字，最后按位置补
            name = str(item.get("name") or "").strip()
            if not FEISHU_NAME.fullmatch(name):
                name = str(old.get("name") or "").strip()
            payload.append(
                {
                    "name": name or _feishu_name(index + 1),
                    "enabled": bool(item.get("enabled")),
                    "app_id": str(item.get("app_id") or "").strip(),
                    "app_secret": secret or str(old.get("app_secret") or ""),
                    "push_enabled": bool(item.get("push_enabled", True)),
                    "remark": str(item.get("remark") or ""),
                }
            )
        await self._config.set(FEISHU_KEY, payload, encrypted=True)
        return self._feishu_instances_view()


def _feishu_name(index: int) -> str:
    return "feishu" if index == 1 else f"feishu_{index}"


def _instance_name(cfg: dict[str, Any], index: int) -> str:
    """实例名跟随配置行保存，增删应用不会让已绑定的渠道名错位；旧行没有名字时按位置补。"""
    name = str(cfg.get("name") or "").strip()
    return name if FEISHU_NAME.fullmatch(name) else _feishu_name(index)


class ChannelNotifier:
    """把提案 / 告警推到已绑定的 IM 渠道，按成员限流。"""

    def __init__(self, *, session_factory, manager: ChannelManager, config) -> None:
        self._session_factory = session_factory
        self._manager = manager
        self._config = config
        self._last_push: dict[int, float] = {}

    async def notify_proposal(self, member, proposal: dict[str, Any]) -> bool:
        if not self._push_allowed(member, "proposal"):
            return False
        from app.channels.render import render_proposal_push

        return await self._fan_out(member, render_proposal_push(proposal))

    async def notify_alert(
        self, member, *, entity_id: str, suggestion: str, risk_level: str = "high"
    ) -> bool:
        if not self._push_allowed(member, "alert"):
            return False
        from app.channels.render import render_alert_push

        return await self._fan_out(
            member,
            render_alert_push(
                entity_id=entity_id, suggestion=suggestion, risk_level=risk_level
            ),
        )

    def _push_allowed(self, member, kind: str) -> bool:
        if not self._config.get("channels.push_enabled", True):
            return False
        key = "channels.notify_min_interval_seconds"
        interval = float(
            self._config.get(
                key,
                self._config.global_settings().get("notify_min_interval_seconds", 300),
            )
        )
        if kind == "alert":
            interval = min(interval, 60.0)
        now = time.monotonic()
        last = self._last_push.get(member.id, 0.0)
        if now - last < interval:
            return False
        self._last_push[member.id] = now
        return True

    async def _fan_out(self, member, message) -> bool:
        from sqlalchemy import select

        from app.db.models import ChannelIdentity

        dispatcher = self._manager.dispatcher
        async with self._session_factory() as session:
            identities = (
                await session.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.member_id == member.id,
                        ChannelIdentity.enabled.is_(True),
                    )
                )
            ).scalars().all()
        sent = False
        # identity.channel 是实例名（feishu / feishu_2 …），推送开关按实例配置取
        instances = {
            _instance_name(cfg, index): cfg
            for index, cfg in enumerate(feishu_instances(self._config.get(FEISHU_KEY)), start=1)
        }
        for identity in identities:
            adapter = dispatcher.adapters().get(identity.channel)
            if adapter is None:
                continue
            if identity.channel.startswith("feishu"):
                cfg = instances.get(identity.channel) or {}
                if not cfg.get("push_enabled", True):
                    continue
            if await dispatcher.push(identity.channel, identity.external_id, message):
                sent = True
        return sent
