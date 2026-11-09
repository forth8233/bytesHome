from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.context import ContextBuilder
from app.agent.schema import AgentProposal
from app.core.logging import scrub_text
from app.db.models import Member, ReasoningTrace
from app.events.models import ProcessedEvent
from app.llm.client import LLMClient, LLMResult
from app.safety.guard import RISK_RANK, ActionGuard

logger = structlog.get_logger(__name__)

STALE_SECONDS = 180


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Reasoner:
    """总线消费者：上下文召回 → LLM → 结构化校验 → 风险合并 → 提案。"""

    def __init__(
        self,
        *,
        config,
        queue: asyncio.Queue[ProcessedEvent],
        builder: ContextBuilder,
        client: LLMClient,
        guard: ActionGuard,
        proposals,
        session_factory: async_sessionmaker[AsyncSession],
        sse=None,
        notifier=None,
    ) -> None:
        self._config = config
        self._queue = queue
        self._builder = builder
        self._client = client
        self._guard = guard
        self._proposals = proposals
        self._session_factory = session_factory
        self._sse = sse
        self._notifier = notifier
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._calls: deque[float] = deque()
        self._target_member_id: int | None = None
        self.counters: Counter[str] = Counter()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="agent-reasoner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await asyncio.wait_for(self._handle(event), timeout=90.0)
            except asyncio.TimeoutError:
                self.counters["timeout"] += 1
                logger.warning("reasoner.timeout", entity_id=event.entity_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单个事件失败不能终止消费
                self.counters["error"] += 1
                logger.exception("reasoner.failed", entity_id=event.entity_id)

    async def _handle(self, event: ProcessedEvent) -> None:
        if not self._config.global_settings().get("active_reasoning_enabled", True):
            self.counters["skipped_disabled"] += 1
            return
        if event.occurred_at is not None:
            age = (_utcnow() - event.occurred_at.replace(tzinfo=None)).total_seconds()
            if age > STALE_SECONDS:
                self.counters["skipped_stale"] += 1
                return
        if not self._rate_allowed():
            self.counters["skipped_rate_limit"] += 1
            return

        member_id = await self._target_member()
        bundle = await self._builder.build(event, member_id=member_id)
        result = await self._client.structured_with_fallback(
            bundle.messages, AgentProposal, schema_name="agent_proposal"
        )
        trace_id = await self._write_trace(result, bundle, event)

        proposal = result.data
        if not result.parsed_ok or not isinstance(proposal, dict):
            self.counters["llm_failed"] += 1
            logger.info("reasoner.parse_failed", error=result.error, attempts=result.attempts)
            return

        parsed = AgentProposal.model_validate(proposal)
        if not parsed.should_propose:
            if event.risk_pre == "high":
                await self._raise_alert(event, parsed, trace_id, member_id)
            self.counters["no_proposal"] += 1
            return

        actions = [action.model_dump() for action in parsed.actions]
        guard = await self._guard.check(
            actions,
            member=None,
            source="proposal",
            event_pre_risk=event.risk_pre,
            llm_risk=parsed.risk_hint,
        )
        if not guard.passed:
            self.counters["guard_denied"] += 1
            logger.info(
                "reasoner.guard_denied",
                entity_id=event.entity_id,
                reasons=guard.reasons[:3],
            )
            return

        if await self._proposals.in_cooldown(member_id, guard.signatures):
            self.counters["cooldown"] += 1
            logger.info("reasoner.cooldown", entity_id=event.entity_id)
            return

        record, outcome = await self._proposals.create(
            entity_id=event.entity_id,
            event_type=event.event_type,
            actions=guard.actions,
            thought=parsed.thought,
            suggestion=parsed.suggestion,
            risk_level=guard.risk_level,
            risk_source=self._risk_source(guard.risk_level, parsed.risk_hint, event.risk_pre),
            trace_id=trace_id,
            member_id=member_id,
        )
        if record is None:
            return

        await self._publish("proposal.created", record)
        self.counters["proposed"] += 1
        logger.info(
            "reasoner.proposed",
            proposal_id=record["id"],
            entity_id=event.entity_id,
            risk=record["risk_level"],
        )
        if self._notifier is not None and member_id is not None:
            member = await self._load_member(member_id)
            if member is not None:
                await self._notifier.notify_proposal(member, record)

    async def _raise_alert(
        self, event: ProcessedEvent, parsed: AgentProposal, trace_id: str, member_id: int | None
    ) -> None:
        """高风险但无可执行动作时转为告警，不能静默丢弃。"""
        suggestion = (
            parsed.suggestion.strip()
            or f"检测到高风险事件（{event.entity_name or event.entity_id}），请人工确认"
        )
        await self._publish(
            "alert",
            {
                "entity_id": event.entity_id,
                "entity_name": event.entity_name,
                "risk_level": "high",
                "suggestion": suggestion,
                "trace_id": trace_id,
            },
        )
        self.counters["alerted"] += 1
        logger.warning("reasoner.alert", entity_id=event.entity_id, suggestion=suggestion[:120])
        if self._notifier is not None and member_id is not None:
            member = await self._load_member(member_id)
            if member is not None:
                await self._notifier.notify_alert(
                    member, entity_id=event.entity_id, suggestion=suggestion
                )

    def _rate_allowed(self) -> bool:
        limit = int(self._config.global_settings().get("llm_rate_limit_per_minute", 30))
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= limit:
            return False
        self._calls.append(now)
        return True

    async def _target_member(self) -> int | None:
        if self._target_member_id is not None:
            return self._target_member_id
        async with self._session_factory() as session:
            member_id = await session.scalar(
                select(Member.id)
                .where(Member.role == "admin", Member.enabled.is_(True))
                .order_by(Member.id)
                .limit(1)
            )
        self._target_member_id = int(member_id) if member_id is not None else None
        return self._target_member_id

    async def _load_member(self, member_id: int) -> Member | None:
        async with self._session_factory() as session:
            return await session.get(Member, member_id)

    async def _write_trace(self, result: LLMResult, bundle, event: ProcessedEvent) -> str:
        trace_id = uuid.uuid4().hex
        try:
            async with self._session_factory() as session:
                session.add(
                    ReasoningTrace(
                        trace_id=trace_id,
                        kind="proposal",
                        provider=result.provider,
                        model=result.model,
                        input_summary=bundle.input_summary,
                        input_tokens=result.prompt_tokens,
                        output_tokens=result.completion_tokens,
                        raw_output=scrub_text(result.content),
                        parsed_ok=result.parsed_ok,
                        duration_ms=result.duration_ms,
                        error=scrub_text(result.error, 1000),
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计写失败不影响提案链路
            logger.exception("reasoner.trace_failed", entity_id=event.entity_id)
        return trace_id

    @staticmethod
    def _risk_source(final: str, llm: str, pre: str) -> str:
        rank = RISK_RANK.get(final, 0)
        contributions = [
            name for name, level in (("llm", llm), ("code", pre)) if RISK_RANK.get(level, 0) == rank
        ]
        if len(contributions) > 1:
            return "merged"
        if contributions:
            return contributions[0]
        return "code"

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._sse is None:
            return
        try:
            await self._sse.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - 推送失败不影响主流程
            logger.exception("reasoner.sse_failed")

    def stats(self) -> dict[str, int]:
        return dict(self.counters)
