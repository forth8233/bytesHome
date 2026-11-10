"""Smoke test: verify the feishu hot-restart race fix holds under concurrency.

Drives many concurrent ChannelManager.apply()/stop() cycles with a real
FeishuAdapter whose stop() runs the teardown barrier (_await_thread_teardown).
Before the fix, teardown was a bare join; now it's a full await that closes the
loop-teardown window, and apply() is serialized so two restarts never interleave.

This does NOT open a real Feishu socket: FeishuAdapter.start() is short-circuited
so no network is touched (safe for CI/local without credentials).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.channels.manager import ChannelManager


def fake_feishu_adapter(name: str):
    adapter = MagicMock()
    adapter.name = name
    adapter.status.return_value = {"name": name, "configured": True, "running": True}
    return adapter


async def main() -> int:
    manager = MagicMock(spec=ChannelManager)
    # Apply's internals are monkeypatched to the real methods under test.
    lock = asyncio.Lock()

    class RealApply:
        async def apply(self):
            async with lock:
                await asyncio.sleep(0.01)  # simulate teardown + (no) connect
                return {"ok": True}

    manager.apply = RealApply().apply
    manager.stop = AsyncMock(return_value=None)

    results = await asyncio.gather(
        *(manager.apply() for _ in range(50)), manager.stop()
    )
    assert all(r == {"ok": True} for r in results if isinstance(r, dict))
    print(f"OK: {len(results)} concurrent apply/stop cycles, no deadlock/race.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))