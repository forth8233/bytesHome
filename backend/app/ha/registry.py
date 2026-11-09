from typing import Any

from app.ha.client_ws import HAWebSocketClient


class RegistryCache:
    def __init__(self) -> None:
        self.entities: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.areas: dict[str, dict[str, Any]] = {}
        self.floors: dict[str, dict[str, Any]] = {}

    async def refresh(self, ws: HAWebSocketClient) -> None:
        entities = await ws.command("config/entity_registry/list") or []
        devices = await ws.command("config/device_registry/list") or []
        areas = await ws.command("config/area_registry/list") or []
        self.entities = {
            item["entity_id"]: item for item in entities if isinstance(item, dict) and "entity_id" in item
        }
        self.devices = {item["id"]: item for item in devices if isinstance(item, dict) and "id" in item}
        self.areas = {
            item["area_id"]: item
            for item in areas
            if isinstance(item, dict) and item.get("area_id")
        }
        try:
            floors = await ws.command("config/floor_registry/list") or []
        except Exception:  # noqa: BLE001 - 旧版 HA 没有楼层注册表
            floors = []
        self.floors = {
            item["floor_id"]: item
            for item in floors
            if isinstance(item, dict) and item.get("floor_id")
        }

    def area_of(self, entity_id: str) -> str | None:
        entry = self.entities.get(entity_id) or {}
        if entry.get("area_id"):
            return entry["area_id"]
        device = self.devices.get(entry.get("device_id") or "") or {}
        return device.get("area_id")

    def area_name(self, area_id: str) -> str | None:
        area = self.areas.get(area_id) or {}
        return area.get("name") or None

    def floor_of(self, entity_id: str) -> str | None:
        entry = self.entities.get(entity_id) or {}
        if entry.get("floor_id"):
            return entry["floor_id"]
        device = self.devices.get(entry.get("device_id") or "") or {}
        if device.get("floor_id"):
            return device["floor_id"]
        area_id = entry.get("area_id") or device.get("area_id")
        area = self.areas.get(area_id or "") or {}
        return area.get("floor_id")

    def floor_name(self, floor_id: str) -> str | None:
        floor = self.floors.get(floor_id) or {}
        return floor.get("name") or None

    def device_of(self, entity_id: str) -> str | None:
        entry = self.entities.get(entity_id) or {}
        return entry.get("device_id")
