from typing import Any

import httpx


class HAClientError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HARestClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        try:
            response = await self._client.get("/api/")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_states(self) -> list[dict[str, Any]]:
        try:
            response = await self._client.get("/api/states")
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code != 200:
            raise HAClientError(
                f"HA returned {response.status_code}", status_code=response.status_code
            )
        return response.json()

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(f"/api/states/{entity_id}")
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HAClientError(
                f"HA returned {response.status_code}", status_code=response.status_code
            )
        return response.json()

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        # Device actions are not retried: a duplicated call could open a lock twice.
        try:
            response = await self._client.post(f"/api/services/{domain}/{service}", json=data or {})
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code >= 400:
            raise HAClientError(
                f"HA rejected service call: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            return []

    async def write_automation(self, automation_id: str, config: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"/api/config/automation/config/{automation_id}", json=config
            )
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code >= 400:
            raise HAClientError(
                f"HA rejected automation write: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            return {}

    async def get_automation_config(
        self, automation_id: str
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                f"/api/config/automation/config/{automation_id}"
            )
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise HAClientError(
                f"HA rejected automation read: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    async def list_automation_configs(self) -> list[dict[str, Any]] | None:
        """返回 None 表示该 HA 版本没有列表接口，需要改走实体遍历。"""
        try:
            response = await self._client.get("/api/config/automation/config")
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code in (404, 405):
            return None
        if response.status_code >= 400:
            raise HAClientError(
                f"HA rejected automation list: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def delete_automation(self, automation_id: str) -> None:
        try:
            response = await self._client.delete(
                f"/api/config/automation/config/{automation_id}"
            )
        except httpx.HTTPError as exc:
            raise HAClientError(f"cannot reach HA: {exc}") from exc
        if response.status_code == 404:
            return
        if response.status_code >= 400:
            raise HAClientError(
                f"HA rejected automation delete: {response.text[:200]}",
                status_code=response.status_code,
            )
