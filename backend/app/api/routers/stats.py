from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from app.api.deps import AdminUser, ConfigDep, CurrentUser, DbSession
from app.db.models import Proposal, ReasoningTrace

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
async def stats(
    user: CurrentUser, request: Request, session: DbSession, config: ConfigDep
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Proposal.status, func.count()).group_by(Proposal.status)
        )
    ).all()
    by_status = {status: int(count) for status, count in rows}
    decided = sum(
        by_status.get(key, 0) for key in ("executed", "failed", "rejected", "timeout", "cancelled")
    )
    approved = by_status.get("executed", 0) + by_status.get("failed", 0)

    total = await session.scalar(select(func.count()).select_from(Proposal))
    risk_rows = (
        await session.execute(
            select(Proposal.risk_level, func.count()).group_by(Proposal.risk_level)
        )
    ).all()

    today = datetime.now().strftime("%Y-%m-%d")
    usage = config.get(f"usage.llm.{today}") or {}
    reasoner = getattr(request.app.state, "reasoner", None)
    proposal_service = getattr(request.app.state, "proposal_service", None)
    return {
        "proposal": {
            "total": int(total or 0),
            "by_status": by_status,
            "by_risk": {risk: int(count) for risk, count in risk_rows},
            "approval_rate": round(approved / decided, 4) if decided else 0.0,
        },
        "llm": {
            "date": today,
            "calls": int(usage.get("calls") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "errors": int(usage.get("errors") or 0),
            "error_rate": round(
                int(usage.get("errors") or 0) / int(usage.get("calls") or 1), 4
            )
            if usage
            else 0.0,
        },
        "reasoner": reasoner.stats() if reasoner is not None else {},
        "proposal_service": proposal_service.counters if proposal_service is not None else {},
    }


@router.get("/stats/fastpath")
async def fastpath_stats(user: CurrentUser, request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "fast_path", None)
    if engine is None:
        return {"enabled": False, "available": False}
    payload = engine.stats()
    payload["available"] = True
    return payload


@router.get("/trace/reasoning")
async def list_traces(
    _: AdminUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trace_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    conditions = []
    if trace_id:
        conditions.append(ReasoningTrace.trace_id == trace_id)
    if kind:
        conditions.append(ReasoningTrace.kind == kind)
    total = await session.scalar(
        select(func.count()).select_from(ReasoningTrace).where(*conditions)
    )
    rows = (
        (
            await session.execute(
                select(ReasoningTrace)
                .where(*conditions)
                .order_by(ReasoningTrace.created_at.desc())
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
                "trace_id": row.trace_id,
                "kind": row.kind,
                "provider": row.provider,
                "model": row.model,
                "input_summary": row.input_summary,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "raw_output": row.raw_output,
                "parsed_ok": row.parsed_ok,
                "duration_ms": row.duration_ms,
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }
