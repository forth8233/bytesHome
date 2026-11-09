from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MultiNotifier:
    """把同一份通知分发给多个通道（HA notify + IM 渠道），任一成功即算成功。"""

    def __init__(self, *notifiers) -> None:
        self._notifiers = [n for n in notifiers if n is not None]

    async def notify_proposal(self, member, proposal: dict[str, Any]) -> bool:
        return await self._run("notify_proposal", member, proposal)

    async def notify_alert(
        self, member, *, entity_id: str, suggestion: str, risk_level: str = "high"
    ) -> bool:
        return await self._run(
            "notify_alert",
            member,
            entity_id=entity_id,
            suggestion=suggestion,
            risk_level=risk_level,
        )

    async def _run(self, method: str, member, *args, **kwargs) -> bool:
        sent = False
        for notifier in self._notifiers:
            handler = getattr(notifier, method, None)
            if handler is None:
                continue
            try:
                if await handler(member, *args, **kwargs):
                    sent = True
            except Exception:  # noqa: BLE001 - 单个通道失败不能影响其它通道
                logger.exception("notify.channel_failed", method=method)
        return sent


class HANotifier:
    """离线通知：复用 HA 的 notify 服务，单成员限流并合并待确认数量。"""

    def __init__(self, config, adapter) -> None:
        self._config = config
        self._adapter = adapter
        self._last_sent: dict[int, float] = {}
        self._suppressed: dict[int, int] = {}

    async def notify_proposal(self, member, proposal: dict[str, Any]) -> bool:
        service = (getattr(member, "notify_service", None) or "").strip()
        if not service or not service.startswith("notify."):
            return False

        interval = float(self._config.global_settings().get("notify_min_interval_seconds", 300))
        now = time.monotonic()
        last = self._last_sent.get(member.id, 0.0)
        if now - last < interval:
            self._suppressed[member.id] = self._suppressed.get(member.id, 0) + 1
            return False

        suppressed = self._suppressed.pop(member.id, 0)
        suggestion = str(proposal.get("suggestion") or "")[:120]
        message = (
            f"[{proposal.get('risk_level', 'low')}] "
            f"{proposal.get('entity_id', '')}: {suggestion} (提案 #{proposal.get('id')})"
        )
        if suppressed:
            message += f"，另有 {suppressed} 条待确认提案"

        domain, _, service_name = service.partition(".")
        if not service_name or self._adapter is None or self._adapter.rest is None:
            return False
        try:
            await self._adapter.rest.call_service(domain, service_name, {"message": message})
        except Exception:  # noqa: BLE001 - 通知失败不能影响提案创建
            logger.exception("notify.ha.failed", member_id=member.id)
            return False
        self._last_sent[member.id] = now
        return True

    async def notify_alert(
        self, member, *, entity_id: str, suggestion: str, risk_level: str = "high"
    ) -> bool:
        """高风险但无动作可执行时的纯告警；用更短的限流间隔，避免被提案推送挤掉。"""
        service = (getattr(member, "notify_service", None) or "").strip()
        if not service or not service.startswith("notify."):
            return False

        interval = float(
            self._config.global_settings().get("alert_notify_min_interval_seconds", 60)
        )
        now = time.monotonic()
        if now - self._last_sent.get(member.id, 0.0) < interval:
            return False

        message = f"[{risk_level}][告警] {entity_id}: {str(suggestion or '')[:120]}"
        domain, _, service_name = service.partition(".")
        if not service_name or self._adapter is None or self._adapter.rest is None:
            return False
        try:
            await self._adapter.rest.call_service(domain, service_name, {"message": message})
        except Exception:  # noqa: BLE001 - 通知失败不能影响主链路
            logger.exception("notify.ha.alert_failed", member_id=member.id)
            return False
        self._last_sent[member.id] = now
        return True
