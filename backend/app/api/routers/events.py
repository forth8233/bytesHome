from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.db.models import ProcessedEventLog

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("/stats")
async def event_stats(_: AdminUser, request: Request) -> dict[str, Any]:
    pipeline = getattr(request.app.state, "event_pipeline", None)
    bus = getattr(request.app.state, "event_bus", None)
    sink = getattr(request.app.state, "event_log_sink", None)
    return {
        "pipeline": pipeline.stats() if pipeline is not None else {},
        "pending_debounce": pipeline.pending_count if pipeline is not None else 0,
        "bus_dropped_total": bus.dropped_total if bus is not None else 0,
        "persisted": sink.persisted if sink is not None else 0,
    }


@router.get("")
async def list_events(
    _: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    entity_id: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    conditions = []
    if entity_id:
        conditions.append(ProcessedEventLog.entity_id == entity_id)
    if event_type:
        conditions.append(ProcessedEventLog.event_type == event_type)
    total = await session.scalar(
        select(func.count()).select_from(ProcessedEventLog).where(*conditions)
    )
    rows = (
        (
            await session.execute(
                select(ProcessedEventLog)
                .where(*conditions)
                .order_by(ProcessedEventLog.occurred_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "event_id": row.event_id,
                "entity_id": row.entity_id,
                "event_type": row.event_type,
                "old_state": row.old_state,
                "new_state": row.new_state,
                "risk_pre": row.risk_pre,
                "window_id": row.window_id,
                "dropped_count": row.dropped_count,
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            }
            for row in rows
        ],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }
