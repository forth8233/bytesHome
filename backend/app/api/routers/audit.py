import json
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.db.models import AuditLog

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/log")
async def list_audit_log(
    _: AdminUser,
    session: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = []
    for row in rows:
        try:
            detail = json.loads(row.detail_json or "{}")
        except json.JSONDecodeError:
            detail = {}
        items.append(
            {
                "id": row.id,
                "member_id": row.member_id,
                "action": row.action,
                "target": row.target,
                "detail": detail,
                "ip": row.ip,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return {"items": items, "page": page, "page_size": page_size}
