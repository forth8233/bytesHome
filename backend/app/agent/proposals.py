from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Proposal, UserFeedback

logger = structlog.get_logger(__name__)

STATES = {
    "pending",
    "approved",
    "executing",
    "executed",
    "failed",
    "rejected",
    "timeout",
    "cancelled",
    "superseded",
}
TERMINAL = {"executed", "failed", "rejected", "timeout", "cancelled", "superseded"}


def proposal_signature(actions: list[dict[str, Any]]) -> str:
    blob = json.dumps(actions, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def dedupe_key(entity_id: str, event_type: str, actions: list[dict[str, Any]]) -> str:
    return hashlib.sha1(
        f"{entity_id}|{event_type}|{proposal_signature(actions)}".encode("utf-8")
    ).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProposalError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def serialize(proposal: Proposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "dedupe_key": proposal.dedupe_key,
        "event_log_id": proposal.event_log_id,
        "entity_id": proposal.entity_id,
        "trace_id": proposal.trace_id,
        "source": proposal.source,
        "risk_level": proposal.risk_level,
        "risk_source": proposal.risk_source,
        "thought": proposal.thought,
        "suggestion": proposal.suggestion,
        "actions": _loads(proposal.actions_json),
        "status": proposal.status,
        "member_id": proposal.member_id,
        "expire_at": _iso(proposal.expire_at),
        "approved_at": _iso(proposal.approved_at),
        "executed_at": _iso(proposal.executed_at),
        "operated_at": _iso(proposal.operated_at),
        "execution_result": _loads(proposal.execution_result, raw=True),
        "reject_reason": proposal.reject_reason,
        "created_at": _iso(proposal.created_at),
        "updated_at": _iso(proposal.updated_at),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _loads(value: str | None, *, raw: bool = False) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value if raw else []


class ProposalService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config,
        adapter=None,
        guard=None,
        sse=None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._adapter = adapter
        self._guard = guard
        self._sse = sse
        self.counters: dict[str, int] = {}

    # ---------- 查询 ----------

    async def get(self, proposal_id: int) -> dict[str, Any]:
        async with self._session_factory() as session:
            proposal = await session.get(Proposal, proposal_id)
            if proposal is None:
                raise ProposalError(404, "proposal not found")
            return serialize(proposal)

    async def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: str | None = None,
        risk_level: str | None = None,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = max(1, min(100, page_size))
        conditions = []
        if status:
            conditions.append(Proposal.status == status)
        if risk_level:
            conditions.append(Proposal.risk_level == risk_level)
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(Proposal).where(*conditions)
            )
            rows = (
                (
                    await session.execute(
                        select(Proposal)
                        .where(*conditions)
                        .order_by(Proposal.created_at.desc())
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "items": [serialize(row) for row in rows],
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
        }

    # ---------- 创建 ----------

    async def create(
        self,
        *,
        entity_id: str,
        event_type: str,
        actions: list[dict[str, Any]],
        thought: str,
        suggestion: str,
        risk_level: str,
        risk_source: str,
        trace_id: str,
        member_id: int | None,
        source: str = "proposal",
        event_log_id: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        settings = self._config.global_settings()
        now = _utcnow()
        key = dedupe_key(entity_id, event_type, actions)
        dedupe_seconds = int(settings.get("proposal_dedupe_seconds", 600))
        timeout_seconds = int(settings.get("proposal_timeout_seconds", 900))

        async with self._session_factory() as session:
            existing = (
                (
                    await session.execute(
                        select(Proposal).where(
                            Proposal.dedupe_key == key, Proposal.status == "pending"
                        )
                    )
                )
                .scalars()
                .all()
            )
            cutoff = now - timedelta(seconds=dedupe_seconds)
            if any(row.created_at >= cutoff for row in existing):
                self._count("duplicate")
                return None, "duplicate"
            for row in existing:
                await self._cas(session, row.id, "pending", "superseded")

            proposal = Proposal(
                dedupe_key=key,
                event_log_id=event_log_id,
                entity_id=entity_id,
                trace_id=trace_id,
                source=source,
                risk_level=risk_level,
                risk_source=risk_source,
                thought=thought,
                suggestion=suggestion,
                actions_json=json.dumps(actions, ensure_ascii=False),
                status="pending",
                member_id=member_id,
                expire_at=now + timedelta(seconds=timeout_seconds),
                created_at=now,
                updated_at=now,
            )
            session.add(proposal)
            await session.commit()
            await session.refresh(proposal)
            self._count("created")
            return serialize(proposal), "created"

    async def in_cooldown(self, member_id: int | None, signatures: list[str]) -> bool:
        if member_id is None or not signatures:
            return False
        settings = self._config.global_settings()
        threshold = int(settings.get("feedback_reject_threshold", 2))
        cooldown = int(settings.get("feedback_cooldown_seconds", 3600))
        cutoff = _utcnow() - timedelta(seconds=cooldown)
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(UserFeedback).where(
                            UserFeedback.member_id == member_id,
                            UserFeedback.action_signature.in_(tuple(set(signatures))),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return any(
            row.consecutive_rejects >= threshold and row.last_triggered_at >= cutoff
            for row in rows
        )

    # ---------- 审批 ----------

    async def approve(self, proposal_id: int, member) -> dict[str, Any]:
        now = _utcnow()
        async with self._session_factory() as session:
            proposal = await session.get(Proposal, proposal_id)
            if proposal is None:
                raise ProposalError(404, "proposal not found")
            if not await self._cas(
                session, proposal.id, "pending", "approved", approved_at=now
            ):
                raise ProposalError(409, "proposal was already handled")
            await session.refresh(proposal)
            actions = _loads(proposal.actions_json)
            event_type = "proposal"
            trace_suggestion = proposal.suggestion

        if self._guard is None or self._adapter is None:
            await self._fail(proposal_id, "execution is not available")
            raise ProposalError(503, "execution is not available")

        guard = await self._guard.check(actions, member=member, source="proposal")
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:500]
            await self._fail(proposal_id, reason)
            raise ProposalError(422, f"action guard rejected: {reason}")

        if not await self._cas(None, proposal_id, "approved", "executing"):
            raise ProposalError(409, "proposal was already handled")

        results: list[dict[str, Any]] = []
        ok = True
        for action in actions:
            payload = dict(action.get("data") or {})
            if action.get("entity_id"):
                payload["entity_id"] = action["entity_id"]
            try:
                output = await self._adapter.call_service(
                    str(action.get("domain")), str(action.get("service")), payload
                )
                results.append({"action": action, "ok": True, "result": output})
            except Exception as exc:  # noqa: BLE001 - 单条失败即整体失败，不自动重试
                ok = False
                results.append({"action": action, "ok": False, "error": str(exc)[:300]})
                break

        final_status = "executed" if ok else "failed"
        await self._cas(
            None,
            proposal_id,
            "executing",
            final_status,
            executed_at=_utcnow() if ok else None,
            execution_result=json.dumps(results, ensure_ascii=False)[:4000],
        )
        if member is not None:
            await self.record_feedback(
                member.id,
                guard.signatures,
                event_type,
                "approved" if ok else "failed",
            )
        updated = await self.get(proposal_id)
        self._count(final_status)
        await self._publish("proposal.updated", updated)
        logger.info(
            "proposal.executed",
            proposal_id=proposal_id,
            status=final_status,
            suggestion=trace_suggestion,
        )
        return updated

    async def reject(self, proposal_id: int, member, reason: str | None = None) -> dict[str, Any]:
        now = _utcnow()
        async with self._session_factory() as session:
            proposal = await session.get(Proposal, proposal_id)
            if proposal is None:
                raise ProposalError(404, "proposal not found")
            signatures = self._signatures_of(proposal)
            if not await self._cas(
                session,
                proposal_id,
                "pending",
                "rejected",
                reject_reason=(reason or "")[:255],
                operated_at=now,
            ):
                raise ProposalError(409, "proposal was already handled")
        if member is not None:
            await self.record_feedback(member.id, signatures, "proposal", "rejected")
        updated = await self.get(proposal_id)
        self._count("rejected")
        await self._publish("proposal.updated", updated)
        return updated

    async def cancel(self, proposal_id: int, member) -> dict[str, Any]:
        async with self._session_factory() as session:
            proposal = await session.get(Proposal, proposal_id)
            if proposal is None:
                raise ProposalError(404, "proposal not found")
            if not await self._cas(session, proposal_id, "pending", "cancelled", operated_at=_utcnow()):
                raise ProposalError(409, "proposal was already handled")
        updated = await self.get(proposal_id)
        self._count("cancelled")
        await self._publish("proposal.updated", updated)
        return updated

    # ---------- 超时扫描 ----------

    async def sweep_expired(self) -> int:
        now = _utcnow()
        expired_ids: list[int] = []
        async with self._session_factory() as session:
            ids = list(
                (
                    await session.execute(
                        select(Proposal.id).where(
                            Proposal.status == "pending", Proposal.expire_at < now
                        )
                    )
                )
                .scalars()
                .all()
            )
            for proposal_id in ids:
                if await self._cas(session, proposal_id, "pending", "timeout", operated_at=now):
                    expired_ids.append(proposal_id)
        for proposal_id in expired_ids:
            await self._publish(
                "proposal.updated", {"proposal_id": proposal_id, "status": "timeout"}
            )
        if expired_ids:
            self._count("timeout", len(expired_ids))
            logger.info("proposal.timeout", count=len(expired_ids))
        return len(expired_ids)

    async def run_sweeper(self, interval: float = 30.0) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self.sweep_expired()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 扫描失败不能终止后台任务
                logger.exception("proposal.sweep_failed")

    # ---------- 反馈记忆 ----------

    async def record_feedback(
        self,
        member_id: int,
        signatures: list[str],
        event_type: str,
        decision: str,
    ) -> None:
        if not signatures:
            return
        rejected = decision == "rejected"
        now = _utcnow()
        try:
            async with self._session_factory() as session:
                for signature in dict.fromkeys(signatures):
                    row = await session.scalar(
                        select(UserFeedback).where(
                            UserFeedback.member_id == member_id,
                            UserFeedback.action_signature == signature,
                            UserFeedback.event_type == event_type,
                        )
                    )
                    if row is None:
                        session.add(
                            UserFeedback(
                                member_id=member_id,
                                event_type=event_type,
                                action_signature=signature,
                                decision=decision,
                                count=1,
                                consecutive_rejects=1 if rejected else 0,
                                last_triggered_at=now,
                            )
                        )
                    else:
                        row.count += 1
                        row.decision = decision
                        row.consecutive_rejects = row.consecutive_rejects + 1 if rejected else 0
                        row.last_triggered_at = now
                await session.commit()
        except Exception:  # noqa: BLE001 - 反馈写失败不影响审批结果
            logger.exception("proposal.feedback.write_failed")

    # ---------- 内部 ----------

    @staticmethod
    def _signatures_of(proposal: Proposal) -> list[str]:
        from app.safety.guard import action_signature

        signatures = []
        for action in _loads(proposal.actions_json):
            try:
                service = f"{action['domain']}.{action['service']}"
            except (KeyError, TypeError):
                continue
            signatures.append(
                action_signature(service, action.get("entity_id"), action.get("data") or {})
            )
        return signatures

    async def _cas(
        self,
        session: AsyncSession | None,
        proposal_id: int,
        expected: str,
        new: str,
        **values: Any,
    ) -> bool:
        if session is None:
            async with self._session_factory() as own_session:
                return await self._cas(own_session, proposal_id, expected, new, **values)
        values.setdefault("operated_at", _utcnow())
        values.setdefault("updated_at", _utcnow())
        result = await session.execute(
            update(Proposal)
            .where(Proposal.id == proposal_id, Proposal.status == expected)
            .values(status=new, **values)
        )
        await session.commit()
        return bool(result.rowcount)

    async def _fail(self, proposal_id: int, reason: str) -> None:
        await self._cas(
            None,
            proposal_id,
            "approved",
            "failed",
            execution_result=reason[:4000],
        )
        updated = await self.get(proposal_id)
        await self._publish("proposal.updated", updated)

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._sse is None:
            return
        try:
            await self._sse.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - 推送失败不影响主流程
            logger.exception("proposal.sse.publish_failed")

    def _count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount
