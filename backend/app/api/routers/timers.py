from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.db.models import Member, ScheduledRun, ScheduledTask
from app.services.audit import add_audit

router = APIRouter(prefix="/api/timers", tags=["timers"])


def _task_dict(task: ScheduledTask, username: str = "") -> dict[str, Any]:
    return {
        "id": task.id,
        "kind": task.kind,
        "text": task.text,
        "summary": task.summary,
        "run_at": task.run_at.isoformat() if task.run_at else None,
        "status": task.status,
        "result_text": task.result_text,
        "fired_at": task.fired_at.isoformat() if task.fired_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "member_id": task.member_id,
        "username": username,
    }


@router.get("")
async def list_timers(
    user: CurrentUser,
    session: DbSession,
    status_filter: str | None = Query(default="pending", max_length=16),
) -> dict[str, Any]:
    stmt = select(ScheduledTask).order_by(ScheduledTask.run_at)
    if status_filter:
        stmt = stmt.where(ScheduledTask.status == status_filter)
    rows = (await session.scalars(stmt)).all()
    members = {
        m.id: m.username
        for m in (
            await session.scalars(
                select(Member).where(Member.id.in_([r.member_id for r in rows if r.member_id]))
            )
        ).all()
    }
    return {"items": [_task_dict(row, members.get(row.member_id or 0, "")) for row in rows]}


@router.get("/runs")
async def list_runs(
    user: CurrentUser,
    session: DbSession,
    task_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    stmt = select(ScheduledRun).order_by(ScheduledRun.id.desc()).limit(limit)
    if task_id is not None:
        stmt = stmt.where(ScheduledRun.task_id == task_id)
    runs = (await session.scalars(stmt)).all()
    tasks = {
        t.id: t
        for t in (
            await session.scalars(
                select(ScheduledTask).where(
                    ScheduledTask.id.in_([r.task_id for r in runs])
                )
            )
        ).all()
    }
    items = []
    for run in runs:
        task = tasks.get(run.task_id)
        items.append(
            {
                "id": run.id,
                "task_id": run.task_id,
                "summary": task.summary if task else "",
                "text": task.text if task else "",
                "status": run.status,
                "detail": run.detail_json if run.detail_json else "{}",
                "fired_at": run.fired_at.isoformat() if run.fired_at else None,
            }
        )
    return {"items": items}


@router.delete("/{task_id}")
async def cancel_timer(
    user: AdminUser,
    session: DbSession,
    task_id: int,
) -> dict[str, str]:
    task = await session.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "定时任务不存在")
    if task.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "仅待执行的定时可取消")
    task.status = "cancelled"
    task.result_text = "已提前取消"
    add_audit(
        session,
        member_id=user.id,
        action="scheduled_task.cancel",
        target=task.summary,
        detail={"task_id": task.id, "kind": task.kind},
    )
    await session.commit()
    return {"status": "ok", "message": "定时已取消"}