import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import structlog

from app.events.models import RawEvent
from app.ha.client_rest import HAClientError, HARestClient
from app.ha.client_ws import HAWebSocketClient
from app.ha.registry import RegistryCache
from app.ha.state_cache import StateCache
from app.services.config_service import ConfigService

logger = structlog.get_logger(__name__)

EventSink = Callable[[RawEvent], Awaitable[None]]


class HAAdapter:
    def __init__(self, config: ConfigService, call_timeout_seconds: float = 15.0) -> None:
        self._config = config
        self._call_timeout = max(1.0, float(call_timeout_seconds))
        self.cache = StateCache()
        self.registry = RegistryCache()
        self.rest: HARestClient | None = None
        self.ws: HAWebSocketClient | None = None
        self.event_sink: EventSink | None = None
        self.last_snapshot_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return bool(self.ws and self.ws.connected)

    async def start(self) -> None:
        base_url, token = self._config.ha_settings()
        if not token:
            logger.warning("ha.token_missing", hint="set HA_TOKEN or POST /api/config/ha")
            return
        self.rest = HARestClient(base_url, token)
        if not await self.rest.ping():
            logger.warning("ha.rest.ping_failed", base_url=base_url)
        self.ws = HAWebSocketClient(base_url, token, self._handle_event, self._on_connected)
        await self.ws.start()

    async def stop(self) -> None:
        if self.ws is not None:
            await self.ws.stop()
        if self.rest is not None:
            await self.rest.close()

    async def _on_connected(self) -> None:
        try:
            if self.ws is None or self.rest is None:
                return
            await self.registry.refresh(self.ws)
            await self.resync()
        except Exception as exc:  # noqa: BLE001 - reconnect loop must survive
            self.last_error = str(exc)
            logger.exception("ha.snapshot.failed")

    async def resync(self) -> int:
        if self.rest is None:
            return 0
        states = await self.rest.get_states()
        rules = self._config.whitelist()
        allowed = [item for item in states if rules.entity_allowed(item.get("entity_id", ""))]
        await self.cache.replace_all(allowed)
        self.last_snapshot_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.info(
            "ha.snapshot.loaded",
            allowed=len(allowed),
            total=len(states),
            registry_entities=len(self.registry.entities),
        )
        return len(allowed)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        if not entity_id:
            return
        if not self._config.whitelist().entity_allowed(entity_id):
            return
        new_state = data.get("new_state")
        if new_state is None:
            await self.cache.remove(entity_id)
        else:
            await self.cache.upsert(new_state)
        if self.event_sink is not None:
            await self.event_sink(
                RawEvent(
                    event_id=str((event.get("context") or {}).get("id") or ""),
                    entity_id=entity_id,
                    old_state=data.get("old_state"),
                    new_state=new_state,
                )
            )

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self.rest is None:
            raise HAClientError("HA REST client is not initialized")
        rules = self._config.whitelist()
        full_name = f"{domain}.{service}"
        entity_id = (data or {}).get("entity_id")
        entity_ids = entity_id if isinstance(entity_id, list) else [entity_id] if entity_id else []
        for item in entity_ids:
            if not rules.entity_allowed(str(item)):
                raise HAClientError(f"entity {item} is not whitelisted")
        if not rules.service_allowed(full_name):
            raise HAClientError(f"service {full_name} is not whitelisted")
        param_error = rules.validate_params(full_name, data or {})
        if param_error:
            raise HAClientError(param_error)
        try:
            return await asyncio.wait_for(
                self.rest.call_service(domain, service, data),
                timeout=self._call_timeout,
            )
        except asyncio.TimeoutError:
            # HA 可能已经把动作下发到设备，只是确认超时；不能报告“未执行”
            raise HAClientError(
                f"Home Assistant did not acknowledge {full_name} within "
                f"{self._call_timeout:.0f}s; the device may still have changed"
            ) from None

    async def visible_snapshot(self) -> list[dict[str, Any]]:
        return await self.cache.all()

    @staticmethod
    async def test_connection(base_url: str, token: str) -> tuple[bool, str]:
        client = HARestClient(base_url, token)
        try:
            ok = await client.ping()
            return (True, "connected") if ok else (False, "HA responded with an error")
        except Exception as exc:  # noqa: BLE001 - surfaced to the API caller
            return False, str(exc)
        finally:
            await client.close()
