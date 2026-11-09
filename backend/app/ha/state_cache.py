import asyncio
from typing import Any


class StateCache:
    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def replace_all(self, states: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._states = {item["entity_id"]: item for item in states}

    async def upsert(self, state: dict[str, Any]) -> None:
        async with self._lock:
            self._states[state["entity_id"]] = state

    async def remove(self, entity_id: str) -> None:
        async with self._lock:
            self._states.pop(entity_id, None)

    async def get(self, entity_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return self._states.get(entity_id)

    async def list_by_domain(self, domain: str) -> list[dict[str, Any]]:
        async with self._lock:
            return [s for eid, s in self._states.items() if eid.split(".", 1)[0] == domain]

    async def all(self) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._states.values())

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        async with self._lock:
            return dict(self._states)

    def __len__(self) -> int:
        return len(self._states)
