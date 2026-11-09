"""应用内 HA 适配器联调脚本。

直接实例化 app.ha.adapter.HAAdapter 打真实 HA，验证：
适配器启动、状态快照白名单过滤、state_changed 事件实时更新缓存、
强制断线后的自动重连与快照补齐。

用法（在 backend 目录执行）：
    ./.venv/Scripts/python.exe ../tools/ha_adapter_check.py --listen 20
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, default=20, help="事件采集秒数")
    parser.add_argument("--wait", type=int, default=12, help="重连等待秒数")
    args = parser.parse_args()

    import os

    os.chdir(BACKEND)

    from app.core.logging import configure_logging
    from app.db.session import SessionFactory, engine
    from app.events.bus import EventBus
    from app.events.models import RawEvent
    from app.events.pipeline import EventPipeline
    from app.ha.adapter import HAAdapter
    from app.services.config_service import ConfigService

    configure_logging("WARNING")

    config = ConfigService(SessionFactory)
    await config.load()

    raws: list[RawEvent] = []
    adapter = HAAdapter(config)
    bus = EventBus()
    await bus.start()
    processed_queue = bus.subscribe("check")
    pipeline = EventPipeline(config, adapter.registry, bus)
    await pipeline.start()

    async def collect(event: RawEvent) -> None:
        raws.append(event)
        await pipeline.handle(event)

    adapter.event_sink = collect

    print(f"启动适配器 -> {config.ha_settings()[0]}")
    await adapter.start()
    try:
        await asyncio.sleep(3)
        if not adapter.connected:
            print("[FAIL] WebSocket 未连接")
            return 1
        print(f"[OK]   已连接，缓存实体 {len(adapter.cache)} 个")
        print(f"[OK]   实体注册表 {len(adapter.registry.entities)} 条")

        print(f"\n[事件] 采集 {args.listen}s")
        await asyncio.sleep(args.listen)
        for event in raws[:10]:
            old = (event.old_state or {}).get("state")
            new = (event.new_state or {}).get("state")
            print(f"        {event.entity_id}: {old} -> {new}")
        print(f"        原始事件 {len(raws)} 条")

        processed = []
        while not processed_queue.empty():
            processed.append(processed_queue.get_nowait())
        merged = sum(item.dropped_count for item in processed)
        print(f"        标准化后 {len(processed)} 条，窗口合并掉 {merged} 条")
        for item in processed[:8]:
            print(
                f"        [{item.event_type}] {item.entity_id} "
                f"{item.old_state} -> {item.new_state} risk={item.risk_pre} "
                f"merged={item.dropped_count}"
            )
        print(f"        流水线计数 {pipeline.stats()}")
        if not raws:
            print("[WARN] 窗口内无事件")
            return 1

        target = raws[-1].entity_id
        cached = await adapter.cache.get(target)
        async with __import__("httpx").AsyncClient(
            base_url=config.ha_settings()[0],
            headers={"Authorization": f"Bearer {config.ha_settings()[1]}"},
            timeout=10,
        ) as client:
            live = (await client.get(f"/api/states/{target}")).json().get("state")
        cached_state = (cached or {}).get("state")
        if cached_state == live:
            print(f"[OK]   缓存与 HA 一致: {target} = {live}")
        else:
            print(f"[FAIL] 缓存不一致: {target} cache={cached_state} live={live}")
            return 1

        print(f"\n[重连] 强制断开底层连接，等待自动恢复")
        before_events = len(raws)
        if adapter.ws is not None and adapter.ws._ws is not None:
            await adapter.ws._ws.close()
        await asyncio.sleep(args.wait)
        if not adapter.connected:
            print("[FAIL] 重连失败")
            return 1
        print("[OK]   重连成功")
        print(f"[OK]   重连后快照刷新时间 {adapter.last_snapshot_at}")
        print(f"[INFO] 重连后新增事件 {len(raws) - before_events} 条")
        return 0
    finally:
        await adapter.stop()
        await pipeline.stop()
        await bus.stop()
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
