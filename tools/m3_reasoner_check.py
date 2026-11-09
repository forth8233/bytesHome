"""M3 推理链路联调：用真实配置与真实 LLM 跑一条构造事件。

只生成提案，不执行任何设备动作。

用法（在 backend 目录执行）：
    ./.venv/Scripts/python.exe ../tools/m3_reasoner_check.py \
        --entity binary_sensor.xiao_mi_shui_jin_wei_shi --old off --new on --risk high
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", required=True)
    parser.add_argument("--old", default="off")
    parser.add_argument("--new", default="on")
    parser.add_argument("--risk", default="high", choices=["low", "medium", "high"])
    parser.add_argument("--wait", type=int, default=120)
    args = parser.parse_args()

    import os

    os.chdir(BACKEND)

    from app.agent.context import ContextBuilder
    from app.agent.proposals import ProposalService
    from app.agent.reasoner import Reasoner
    from app.core.logging import configure_logging
    from app.db.session import SessionFactory, engine
    from app.events.models import ProcessedEvent
    from app.ha.adapter import HAAdapter
    from app.llm.client import LLMClient
    from app.notify.sse import SSEHub
    from app.safety.guard import ActionGuard
    from app.services.config_service import ConfigService

    configure_logging("WARNING")

    config = ConfigService(SessionFactory)
    await config.load()
    adapter = HAAdapter(config)
    await adapter.start()
    await asyncio.sleep(4)

    sse_hub = SSEHub()
    guard = ActionGuard(config, SessionFactory)
    proposals = ProposalService(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        sse=sse_hub,
    )
    queue: asyncio.Queue = asyncio.Queue()
    reasoner = Reasoner(
        config=config,
        queue=queue,
        builder=ContextBuilder(
            session_factory=SessionFactory, cache=adapter.cache, registry=adapter.registry
        ),
        client=LLMClient(config),
        guard=guard,
        proposals=proposals,
        session_factory=SessionFactory,
        sse=sse_hub,
    )
    await reasoner.start()

    state = await adapter.cache.get(args.entity)
    if state is None:
        print(f"[FAIL] 缓存中没有实体 {args.entity}")
        await reasoner.stop()
        await adapter.stop()
        await engine.dispose()
        return 1
    attributes = state.get("attributes") or {}
    # 真实链路中缓存先于事件更新；构造事件时必须同步，否则上下文自相矛盾
    state = dict(state)
    state["state"] = args.new
    await adapter.cache.upsert(state)
    event = ProcessedEvent(
        event_id="m3-check",
        entity_id=args.entity,
        entity_name=attributes.get("friendly_name") or args.entity,
        domain=args.entity.split(".", 1)[0],
        event_type="state_changed",
        old_state=args.old,
        new_state=args.new,
        area_id=adapter.registry.area_of(args.entity),
        device_id=adapter.registry.device_of(args.entity),
        occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
        risk_pre=args.risk,
    )
    print(f"构造事件 {args.entity}: {args.old} -> {args.new} risk_pre={args.risk}")
    await queue.put(event)

    done_keys = ("proposed", "no_proposal", "llm_failed", "guard_denied", "cooldown")
    for _ in range(args.wait):
        await asyncio.sleep(1)
        if any(reasoner.counters.get(key) for key in done_keys):
            break

    print(f"推理计数 {reasoner.stats()}")
    for entry in list(sse_hub._ring)[-5:]:
        print(f"SSE #{entry[0]} {entry[1]} {entry[2][:300]}")

    await reasoner.stop()
    await adapter.stop()
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
