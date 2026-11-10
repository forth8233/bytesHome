from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select

from app.db.models import Member, ScheduledRun, ScheduledTask
from app.services.audit import add_audit

log = logging.getLogger(__name__)

# 高频轮询间隔不宜过大：需在该粒度内触发到点的定时
POLL_INTERVAL_SECONDS = 5


class ScheduleRunner:
    """常驻轮询调度器：每轮捞出到点的 pending 任务，后台触发设备动作。

    与手动控制同口径：动作仍过 ActionGuard；调度无人确认，requires_confirm /
    未通过的任务标记 failed 而非执行。逐条 call_service，结果写入 ScheduledRun。
    """

    def __init__(
        self,
        *,
        session_factory,
        adapter,
        guard,
        channel_notifier=None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._guard = guard
        self.notifier = channel_notifier
        self._poll_interval = poll_interval
        self._next_check_at: float | None = None

    @property
    def next_check_at(self) -> float | None:
        return self._next_check_at

    async def run(self) -> None:
        log.info("scheduler.started: interval=%ss", self._poll_interval)
        while True:
            await asyncio.sleep(self._poll_interval)
            self._next_check_at = asyncio.get_event_loop().time() + self._poll_interval
            try:
                if self._adapter.connected:
                    await self._sweep()
            except Exception:  # noqa: BLE001 - 调度器常驻，单轮异常不中断
                log.exception("scheduler.sweep_failed")

    async def _sweep(self) -> None:
        now = _utcnow()
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ScheduledTask).where(
                        ScheduledTask.status == "pending",
                        ScheduledTask.run_at <= now,
                    )
                )
            ).all()
        for task in rows:
            try:
                await self._fire(task)
            except Exception:  # noqa: BLE001 - 单个任务失败不影响其余
                log.exception("scheduler.fire_failed: task_id=%s", task.id)

    async def _fire(self, task: ScheduledTask) -> None:
        actions: list[dict[str, Any]] = json.loads(task.actions_json or "[]")
        if not actions:
            await self._resolve(task, "failed", "任务无可用动作", detail={"reason": "empty actions"})
            await self._notify_task_done(task, "failed", "任务无可用动作", None)
            return

        async with self._session_factory() as session:
            member = await session.get(Member, task.member_id) if task.member_id else None
            if task.member_id is not None and member is None:
                await self._resolve(task, "failed", "发起人不存在", detail={"reason": "member missing"})
                await self._notify_task_done(task, "failed", "发起人不存在", None)
                return

            guard = await self._guard.check(actions, member=member, source="scheduled_task")
            if not guard.passed:
                reason = "; ".join(guard.reasons)[:300]
                await self._resolve(
                    task,
                    "failed",
                    f"安全策略拦截：{reason}",
                    detail={"reason": reason, "guard": guard.reasons},
                )
                await self._notify_task_done(
                    task, "failed", f"安全策略拦截：{reason}", member
                )
                return
            if guard.requires_confirm:
                await self._resolve(
                    task,
                    "failed",
                    "该动作需人工确认，定时不自动执行",
                    detail={"reason": "requires_confirm", "risk_level": guard.risk_level},
                )
                await self._notify_task_done(
                    task, "failed", "该动作需人工确认，定时不自动执行", member
                )
                return

            executed, note = await self._execute(session, actions)
            if not executed:
                await self._resolve(
                    task,
                    "failed",
                    note,
                    detail={"reason": note, "status": "partial"},
                )
                await self._notify_task_done(task, "failed", note, member)
                return
            await self._resolve(
                task,
                "ok",
                note or "已执行",
                detail={"note": note, "actions": actions, "status": "ok"},
                member_id=member.id if member else None,
            )
            await self._notify_task_done(task, "ok", note or "已执行", member)

    async def _notify_task_done(
        self,
        task: ScheduledTask,
        status: str,
        note: str,
        member: "Member | None",
    ) -> None:
        """任务执行后给设定者发回执。

        优先回精确来源会话（source_channel/source_ref），取不到再按成员全渠道 fan-out；
        未接渠道（sender 为 None）直接跳过，不影响本任务结果。
        """
        from app.channels.render import render_schedule_receipt

        notifier = self.notifier
        if notifier is None:
            return
        ok = status == "ok"
        summary = task.summary or task.text or ""
        message = render_schedule_receipt(ok=ok, summary=summary, result_text=note)
        try:
            if task.source_channel and task.source_ref:
                await notifier.send_to(task.source_channel, task.source_ref, message)
            elif member is not None:
                await notifier.push_to_member(member, message)
        except Exception:  # noqa: BLE001 - 回执失败不影响任务结果
            log.exception("scheduler.receipt_failed: task_id=%s", task.id)

    async def _execute(
        self, session, actions: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """逐组 call_service；任一组失败即整体视为失败。返回 (成功?, 备注)。"""
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for action in actions:
            data = action.get("data") or {}
            key = (
                str(action["domain"]),
                str(action["service"]),
                json.dumps(data, sort_keys=True, ensure_ascii=False),
            )
            group = groups.setdefault(
                key,
                {
                    "domain": str(action["domain"]),
                    "service": str(action["service"]),
                    "data": data,
                    "entity_ids": [],
                },
            )
            group["entity_ids"].append(action.get("entity_id"))

        notes: list[str] = []
        for group in groups.values():
            payload = dict(group["data"])
            entity_ids = [e for e in group["entity_ids"] if e]
            if len(entity_ids) == 1:
                payload["entity_id"] = entity_ids[0]
            elif entity_ids:
                payload["entity_id"] = entity_ids
            try:
                output = await self._adapter.call_service(
                    group["domain"], group["service"], payload
                )
                notes.append(f"{group['domain']}.{group['service']} 执行成功")
            except Exception as exc:  # noqa: BLE001 - 超时可能设备已变更，不报失败
                err = str(exc)
                notes.append(f"{group['domain']}.{group['service']}：{err}")
                if "did not acknowledge" in err:
                    # 确认超时：动作可能已下发到设备，仅记录不标失败
                    notes.append("（确认超时，设备可能已变更）")
                    continue
                return False, err[:200]
        return True, "；".join(notes)

    async def _resolve(
        self,
        task: ScheduledTask,
        status: str,
        result_text: str,
        *,
        detail: dict[str, Any] | None = None,
        member_id: int | None = None,
    ) -> None:
        """写 ScheduledRun + 更新 task 状态 + 审计，同一事务提交。"""
        fired = _utcnow()
        async with self._session_factory() as session:
            run = ScheduledRun(
                task_id=task.id,
                member_id=member_id if member_id is not None else task.member_id,
                status=status,
                detail_json=json.dumps(detail or {}, ensure_ascii=False),
                fired_at=fired,
            )
            session.add(run)

            # task 来自此前会话，已脱离；用 merge 重新关联才能持久化状态变更
            task = await session.merge(task)
            task.status = status
            task.fired_at = fired
            task.result_text = str(result_text)[:1000]

            add_audit(
                session,
                member_id=member_id if member_id is not None else task.member_id,
                action=f"scheduled_task.{status}",
                target=task.summary,
                detail={"task_id": task.id, "kind": task.kind, "result": result_text[:200]},
            )
            await session.commit()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)