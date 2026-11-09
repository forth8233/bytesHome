import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def add_audit(
    session: AsyncSession,
    *,
    member_id: int | None,
    action: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
    ip: str = "",
) -> None:
    """审计行与业务改动同事务写入，由调用方统一提交。"""
    session.add(
        AuditLog(
            member_id=member_id,
            action=action,
            target=target[:255],
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
            ip=(ip or "")[:64],
        )
    )
