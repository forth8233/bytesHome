import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
import websockets

logger = structlog.get_logger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class HAWebSocketClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        on_event: EventHandler,
        on_connected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        scheme = "wss" if base_url.startswith("https://") else "ws"
        host = base_url.split("://", 1)[1].rstrip("/")
        self._url = f"{scheme}://{host}/api/websocket"
        self._token = token
        self._on_event = on_event
        self._on_connected = on_connected

        self._ws: websockets.ClientConnection | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="ha-ws-listener")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def command(self, command_type: str, **payload: Any) -> Any:
        ws = self._ws
        if ws is None or not self.connected:
            raise RuntimeError("HA websocket is not connected")
        command_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[command_id] = future
        message = {"id": command_id, "type": command_type, **payload}
        try:
            await ws.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout=15)
        finally:
            self._pending.pop(command_id, None)

    async def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - listener must never die
                self.connected = False
                self.last_error = str(exc)
                logger.warning("ha.ws.connection_lost", error=str(exc), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self._url, max_size=16 * 1024 * 1024, ping_interval=20, ping_timeout=20
        ) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if first.get("type") == "auth_required":
                await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if auth.get("type") != "auth_ok":
                    raise RuntimeError(f"HA websocket auth failed: {auth}")
            elif first.get("type") != "auth_ok":
                raise RuntimeError("unexpected HA websocket handshake")

            self._ws = ws
            self.connected = True
            self.last_error = None
            logger.info("ha.ws.connected")

            reader = asyncio.create_task(self._reader(ws), name="ha-ws-reader")
            try:
                await self.command("subscribe_events", event_type="state_changed")
                if self._on_connected is not None:
                    await self._on_connected()
                await reader
            finally:
                reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass
                self.connected = False
                self._ws = None
                self._fail_pending(RuntimeError("HA websocket closed"))

    async def _reader(self, ws: websockets.ClientConnection) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except ValueError:
                logger.warning("ha.ws.bad_frame")
                continue
            message_type = message.get("type")
            if message_type == "event":
                try:
                    await self._on_event(message.get("event") or {})
                except Exception:  # noqa: BLE001 - one bad event must not break the stream
                    logger.exception("ha.ws.event_handler_failed")
            elif message_type == "result":
                future = self._pending.pop(message.get("id"), None)
                if future is None or future.done():
                    continue
                if message.get("success"):
                    future.set_result(message.get("result"))
                else:
                    future.set_exception(
                        RuntimeError(f"HA command failed: {message.get('error')}")
                    )

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
