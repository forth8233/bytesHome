# -*- coding: utf-8 -*-
"""定时功能端到端冒烟：parse -> 入库 -> scheduler fire -> 执行记录。"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sqlalchemy import delete, select
from types import SimpleNamespace

from app.db.models import Member, ScheduledRun, ScheduledTask
from app.db.session import SessionFactory
from app.fastpath.scheduler import ScheduleRunner
from app.fastpath.schedule import parse_schedule


class FakeAdapter:
    connected = True

    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data)))
        return {"status": "ok"}


class FakeGuard:
    async def check(self, actions, member=None, source=None):
        return SimpleNamespace(
            passed=True, requires_confirm=False, risk_level="low", reasons=[]
        )


async def main():
    adapter = FakeAdapter()
    guard = FakeGuard()
    runner = ScheduleRunner(session_factory=SessionFactory, adapter=adapter, guard=guard)

    async with SessionFactory() as s:
        member = (await s.scalars(select(Member).limit(1))).first()
        member_id = member.id if member else None
    print("member_id:", member_id)

    # 1) 时间解析：各类定时 << 剥离时间词
    for c in ["5分钟后开灯", "凌晨开灯", "11点开灯", "筒灯开5分钟", "开灯"]:
        r = parse_schedule(c)
        info = (
            f"kind={r.kind} action={r.action_text!r} run_at={r.run_at}"
            if r else "None"
        )
        print(f"parse {c!r:16} -> {info}")

    # 2) 入库一条 pending 相对定时（run_at 已过，触发 fire）
    import json
    from datetime import datetime, timedelta, timezone

    actions = [
        {"domain": "light", "service": "turn_on", "entity_id": "light.demo", "data": {}}
    ]
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    async with SessionFactory() as s:
        task = ScheduledTask(
            member_id=member_id,
            kind="relative",
            text="5分钟后开灯",
            summary="5分钟后开灯",
            actions_json=json.dumps(actions, ensure_ascii=False),
            run_at=past,
            status="pending",
        )
        s.add(task)
        await s.commit()
        task_id = task.id
    print("created pending task id:", task_id)

    # 3) 手动触发 fire（等价于调度器轮询到点捞起）
    await runner._fire(task)

    # 4) 校验调度结果
    async with SessionFactory() as s:
        task = await s.get(ScheduledTask, task_id)
        print(
            "after fire: status=", task.status,
            "| fired_at=", task.fired_at,
            "| result=", task.result_text,
        )
        rows = (
            await s.scalars(
                select(ScheduledRun).where(ScheduledRun.task_id == task_id)
            )
        ).all()
        print("runs:", [(r.status, r.detail_json) for r in rows])
    print("adapter calls:", adapter.calls)

    # 5) 清理
    async with SessionFactory() as s:
        await s.execute(delete(ScheduledRun).where(ScheduledRun.task_id == task_id))
        obj = await s.get(ScheduledTask, task_id)
        if obj:
            await s.delete(obj)
        await s.commit()
    print("cleanup done")


asyncio.run(main())