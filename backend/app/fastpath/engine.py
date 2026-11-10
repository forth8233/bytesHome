from __future__ import annotations

import json
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select

from app.db.models import (
    EntityAlias,
    IntentRule,
    MemberEntityPermission,
    ScheduledTask,
    VoiceDeviceProfile,
    VoiceGroupList,
    VoiceLexiconEntry,
)
from app.fastpath.capabilities import FUNCTION_LABELS, build_capability
from app.fastpath.entities import AliasRecord, EntityRef, EntityResolver, Resolution
from app.fastpath.emotional import (
    EmotionalFeeling,
    build_emotional_actions,
    emotional_hint,
    match_emotional,
)
from app.fastpath.grammar import UNIT_BY_DOMAIN, ParsedIntent, parse_choice, parse_intent
from app.fastpath.normalize import normalize
from app.fastpath.schedule import ScheduleSpec, parse_schedule
from app.fastpath.rules import SceneRule, match_scene, scene_from_row
from app.fastpath.voice import VoiceLexicon, VoiceParser, VoiceResult
from app.fastpath.voice_lexicon import LexiconEntry
from app.services.audit import add_audit

logger = structlog.get_logger(__name__)

DEFAULT_FAST_PATH = {
    "enabled": True,
    "state_max_age_seconds": 60,
    "session_entity_ttl_seconds": 120,
    "max_candidates": 3,
    "member_rate_limit_per_minute": 30,
    "voice_enabled": True,
    "voice_max_group_actions": 50,
    "voice_default_steps": {},
    "voice_terminal_floor_id": "",
    "voice_terminal_area_id": "",
}

SESSION_REFS = {"就是它", "这个", "它", "再关一次", "再开一次", "再来一次"}

_STATE_LABELS = {
    "on": "开着",
    "off": "关着",
    "open": "开着",
    "closed": "关着",
    "unavailable": "不可用",
    "unknown": "状态未知",
}

_OFF_SERVICES = {"turn_off", "close_cover", "lock"}
_ON_SERVICES = {"turn_on", "open_cover", "unlock"}


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class RouteResult:
    status: str
    reply: str
    rule_id: int | None = None
    latency_ms: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    hint: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": "fast_path",
            "route_status": self.status,
            "rule_id": self.rule_id,
            "latency_ms": self.latency_ms,
            "reply": self.reply,
            "tool_results": self.tool_results,
            "proposal": self.proposal,
            "candidates": self.candidates,
        }


class FastPathEngine:
    """确定性快路径：规则与别名匹配，不调用 LLM，动作仍经 ActionGuard。"""

    def __init__(
        self,
        *,
        session_factory,
        config,
        adapter,
        guard,
        proposals,
        sse=None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._adapter = adapter
        self._guard = guard
        self._proposals = proposals
        self._sse = sse
        self._aliases: list[AliasRecord] = []
        self._rules: list[SceneRule] = []
        self._voice: VoiceParser | None = None
        self._voice_entries = 0
        self._pending: dict[tuple[int, int], dict[str, Any]] = {}
        self._last: dict[tuple[int, int], dict[str, Any]] = {}
        self._last_voice: dict[tuple[int, int], dict[str, Any]] = {}
        self._rate: dict[int, deque[float]] = {}
        self._counters: Counter[str] = Counter()
        self._latencies: deque[int] = deque(maxlen=200)
        self._rule_hits: Counter[str] = Counter()
        self._fallback_reasons: Counter[str] = Counter()

    async def reload(self) -> None:
        async with self._session_factory() as session:
            alias_rows = (
                await session.scalars(select(EntityAlias).order_by(EntityAlias.id))
            ).all()
            rule_rows = (
                await session.scalars(
                    select(IntentRule)
                    .where(IntentRule.kind == "scene", IntentRule.enabled.is_(True))
                    .order_by(IntentRule.priority.desc(), IntentRule.id)
                )
            ).all()
            voice_rows = (
                await session.scalars(
                    select(VoiceLexiconEntry)
                    .where(VoiceLexiconEntry.enabled.is_(True))
                    .order_by(VoiceLexiconEntry.id)
                )
            ).all()
            profile_rows = (
                await session.scalars(
                    select(VoiceDeviceProfile).where(
                        VoiceDeviceProfile.enabled.is_(True)
                    )
                )
            ).all()
            group_rows = (
                await session.scalars(
                    select(VoiceGroupList)
                    .where(VoiceGroupList.enabled.is_(True))
                    .order_by(VoiceGroupList.id)
                )
            ).all()
        self._aliases = [
            AliasRecord(
                entity_id=row.entity_id,
                alias=row.alias,
                display_name=row.display_name,
                area_id=row.area_id,
                domain=row.domain or row.entity_id.split(".", 1)[0],
                can_control=row.can_control,
            )
            for row in alias_rows
        ]
        rules: list[SceneRule] = []
        for row in rule_rows:
            rule = scene_from_row(row)
            if rule is not None:
                rules.append(rule)
        self._rules = rules
        self._reload_voice(voice_rows, profile_rows, group_rows)
        logger.info(
            "fast_path.reloaded",
            aliases=len(self._aliases),
            rules=len(self._rules),
            voice_entries=self._voice_entries,
        )

    def _reload_voice(self, rows, profile_rows, group_rows=()) -> None:
        if not rows:
            self._voice = None
            self._voice_entries = 0
            return
        entries = [
            LexiconEntry(
                lexicon=row.lexicon,
                term=row.term or "",
                canonical=row.canonical or "",
                alias=row.alias,
                functions=tuple(_json_list(row.functions_json)),
                unit=row.unit or "",
                numeric_value=row.numeric_value,
                entry_type=row.entry_type,
                source=row.source or "xlsx",
            )
            for row in rows
        ]
        profiles: dict[str, dict[str, Any]] = {}
        for row in profile_rows:
            profile: dict[str, Any] = {}
            functions = _json_dict(row.functions_json)
            if isinstance(functions, dict):
                profile.update(functions)
            steps = _json_dict(row.steps_json)
            if steps:
                profile["steps"] = steps
            ranges = _json_dict(row.ranges_json)
            if ranges:
                profile["ranges"] = ranges
            if row.device_type:
                profile["device_type"] = row.device_type
            profiles[row.entity_id] = profile
        group_lists = [
            {
                "device_type": row.device_type or "",
                "floor_id": row.floor_id or "",
                "area_id": row.area_id or "",
                "entity_ids": _json_list(row.entity_ids_json),
            }
            for row in group_rows
        ]
        try:
            lexicon = VoiceLexicon.from_entries(entries)
            parser = VoiceParser(
                lexicon,
                self._aliases,
                getattr(self._adapter, "registry", None),
                profiles,
                group_lists,
            )
        except Exception:  # noqa: BLE001 - 构建失败保留旧快照
            logger.exception("fast_path.voice_reload_failed")
            return
        self._voice = parser
        self._voice_entries = len(entries)

    def _settings(self) -> dict[str, Any]:
        merged = dict(DEFAULT_FAST_PATH)
        value = (self._config.global_settings() or {}).get("fast_path")
        if isinstance(value, dict):
            merged.update(value)
        return merged

    @property
    def enabled(self) -> bool:
        return bool(self._settings().get("enabled", True))

    async def route(
        self,
        member,
        text: str,
        session_id: int | None = None,
        *,
        channel: str = "",
        channel_ref: str = "",
    ) -> RouteResult | None:
        settings = self._settings()
        if not settings.get("enabled", True):
            self._counters["disabled"] += 1
            return None

        started = time.perf_counter()
        key = (member.id, session_id or 0)
        now = time.monotonic()
        states = await self._states()
        resolver = EntityResolver(self._aliases, getattr(self._adapter, "registry", None))
        view_patterns = await self._view_patterns(member)

        pending = self._pending.get(key)
        if pending is not None and pending["expires"] < now:
            self._pending.pop(key, None)
            pending = None
        if pending is not None:
            if pending.get("kind") == "voice_function" and self._voice is not None:
                options = list(pending["slots"].function_options)
                chosen = self._select_voice_function(text, options)
                if chosen is None:
                    pending["expires"] = now + int(
                        settings.get("session_entity_ttl_seconds", 120)
                    )
                    return self._finish(
                        RouteResult(status="clarify", reply=pending["reply"]),
                        started,
                    )
                self._pending.pop(key, None)
                voice_result = self._voice.build_with_function(
                    pending["slots"], chosen, states=states
                )
                return self._finish(
                    await self._handle_voice(
                        member, voice_result, states, settings, key
                    ),
                    started,
                )
            selected = self._select_candidate(text, pending["candidates"])
            if selected is not None:
                if pending.get("kind") == "voice" and self._voice is not None:
                    voice_result = self._voice.build_for_target(
                        pending["slots"], selected, states=states
                    )
                    return self._finish(
                        await self._handle_voice(
                            member, voice_result, states, settings, key
                        ),
                        started,
                    )
                result = await self._control(
                    member,
                    pending["intent"],
                    states,
                    resolver,
                    view_patterns,
                    settings,
                    key,
                    preselected=[selected],
                )
                return self._finish(result, started)

        normalized = normalize(text)
        if normalized in SESSION_REFS:
            last_voice = self._last_voice.get(key)
            if (
                last_voice is not None
                and last_voice["expires"] >= now
                and last_voice["actions"]
            ):
                result = await self._replay_voice(
                    member, last_voice, states, settings, key
                )
                return self._finish(result, started)
            last = self._last.get(key)
            if last is not None and last["expires"] >= now:
                result = await self._control(
                    member,
                    last["intent"],
                    states,
                    resolver,
                    view_patterns,
                    settings,
                    key,
                    preselected=last["entities"],
                )
                return self._finish(result, started)
            self._fallback_reasons["no_session_ref"] += 1
            return None

        rule = match_scene(self._rules, text, exact=True)
        if rule is not None:
            result = await self._scene(
                member, rule, states, resolver, view_patterns, settings
            )
            return self._finish(result, started)

        spec = parse_schedule(text)
        if spec is not None:
            logger.info(
                "fast_path.schedule_hit",
                kind=spec.kind,
                summary=spec.summary,
                action_text=spec.action_text,
                text=text[:80],
            )
            result = await self._handle_schedule(
                member, spec, states, resolver, view_patterns, settings, key,
                channel=channel, channel_ref=channel_ref,
            )
            if result is not None:
                return self._finish(result, started)
            self._counters["schedule_unresolved"] += 1

        if self._voice is not None and settings.get("voice_enabled", True):
            try:
                voice_result = self._voice.parse(
                    text,
                    states=states,
                    resolver=resolver,
                    member=member,
                    view_patterns=view_patterns,
                    settings=settings,
                )
            except Exception:  # noqa: BLE001 - 解析异常退回旧快路径
                logger.exception("fast_path.voice_parse_failed")
                self._counters["voice_error"] += 1
                voice_result = None
            if voice_result is not None:
                return self._finish(
                    await self._handle_voice(
                        member, voice_result, states, settings, key
                    ),
                    started,
                )
            self._counters["voice_none"] += 1

        intent = parse_intent(text)
        if intent is None:
            rule = match_scene(self._rules, text, exact=False)
            if rule is not None:
                result = await self._scene(
                    member, rule, states, resolver, view_patterns, settings
                )
                return self._finish(result, started)
            # 情感类主观感受兜底：无匹配场景时按情感词映射设备动作
            feeling = match_emotional(text)
            if feeling is not None:
                emotional_actions = build_emotional_actions(
                    feeling, states, resolver, view_patterns
                )
                if emotional_actions:
                    result = await self._emotional(
                        member, feeling, emotional_actions,
                        states, settings, key,
                    )
                    if result is not None:
                        return self._finish(result, started)
            self._fallback_reasons["no_match"] += 1
            return None

        if intent.kind == "scene":
            rule = self._scene_by_name(intent.scene or "")
            if rule is None:
                self._fallback_reasons["scene_not_found"] += 1
                return None
            return self._finish(
                await self._scene(member, rule, states, resolver, view_patterns, settings),
                started,
            )

        if intent.kind == "query":
            result = await self._query(
                member, intent, states, resolver, view_patterns
            )
            return self._finish(result, started) if result is not None else None

        result = await self._control(
            member, intent, states, resolver, view_patterns, settings, key
        )
        return self._finish(result, started) if result is not None else None

    async def test(self, text: str, member) -> dict[str, Any]:
        states = await self._states()
        resolver = EntityResolver(self._aliases, getattr(self._adapter, "registry", None))
        view_patterns = await self._view_patterns(member)
        settings = self._settings()
        voice_preview: dict[str, Any] | None = None
        if self._voice is not None and settings.get("voice_enabled", True):
            try:
                voice_preview = self._voice.preview(
                    text,
                    states=states,
                    resolver=resolver,
                    member=member,
                    view_patterns=view_patterns,
                    settings=settings,
                )
            except Exception:  # noqa: BLE001 - 预览失败不影响旧路径预览
                logger.exception("fast_path.voice_preview_failed")
        if (
            voice_preview is not None
            and voice_preview.get("route") == "fast_path"
            and voice_preview.get("actions")
        ):
            guard = await self._guard.check(
                voice_preview["actions"], member=member, source="chat_fast_path"
            )
            voice_preview["guard"] = {
                "passed": guard.passed,
                "requires_confirm": guard.requires_confirm,
                "risk_level": guard.risk_level,
                "reasons": guard.reasons,
            }
        intent = parse_intent(text)
        rule = match_scene(self._rules, text, exact=True) or match_scene(
            self._rules, text, exact=False
        )
        payload: dict[str, Any] = {
            "text": text,
            "scene_rule": {"id": rule.id, "name": rule.name} if rule else None,
            "voice": voice_preview,
        }
        voice_hit = (
            voice_preview is not None and voice_preview.get("route") == "fast_path"
        )
        if intent is None:
            payload["intent"] = None
            emotional: dict[str, Any] | None = None
            feeling = match_emotional(text)
            if feeling is not None:
                actions = build_emotional_actions(
                    feeling, states, resolver, view_patterns
                )
                guard = await self._guard.check(
                    actions, member=member, source="chat_fast_path"
                )
                emotional = emotional_hint(feeling, actions, states)
                emotional["guard"] = {
                    "passed": guard.passed,
                    "requires_confirm": guard.requires_confirm,
                    "risk_level": guard.risk_level,
                    "reasons": guard.reasons,
                }
            payload["emotional"] = emotional
            payload["route"] = (
                "fast_path"
                if rule is not None or voice_hit or (emotional and emotional["targets"])
                else "slow_path"
            )
            return payload
        payload["intent"] = {
            "kind": intent.kind,
            "op": intent.op,
            "domain": intent.domain,
            "service": intent.service,
            "target": intent.target,
            "data": intent.data,
            "query": intent.query,
            "explicit_all": intent.explicit_all,
        }
        if intent.kind == "query":
            if intent.query in ("temperature", "humidity"):
                candidates = self._metric_candidates(intent, states)
                payload["resolution"] = {
                    "status": "ok" if candidates else "none",
                    "entities": [
                        {
                            "entity_id": entity_id,
                            "name": self._friendly(entity_id, states),
                            "area_id": area_id,
                            "domain": entity_id.split(".", 1)[0],
                        }
                        for entity_id, _, area_id in candidates[:10]
                    ],
                    "reason": None if candidates else "no_metric",
                }
                payload["route"] = (
                    "fast_path" if candidates or voice_hit else "slow_path"
                )
                return payload
            query_resolution = resolver.resolve(
                intent.target,
                intent.domain,
                states,
                explicit_all=intent.explicit_all,
                member=member,
                view_patterns=view_patterns,
                want_control=False,
            )
            payload["resolution"] = {
                "status": query_resolution.status,
                "entities": [
                    {
                        "entity_id": ref.entity_id,
                        "name": ref.name,
                        "area_id": ref.area_id,
                        "domain": ref.domain,
                    }
                    for ref in query_resolution.entities
                ],
                "reason": query_resolution.reason,
            }
            payload["route"] = (
                "fast_path"
                if query_resolution.status in ("ok", "ambiguous") or voice_hit
                else "slow_path"
            )
            return payload
        if intent.kind != "control":
            payload["route"] = "fast_path"
            payload["resolution"] = None
            return payload
        resolution = resolver.resolve(
            intent.target,
            intent.domain,
            states,
            explicit_all=intent.explicit_all,
            member=member,
            view_patterns=view_patterns,
            want_control=True,
        )
        payload["resolution"] = {
            "status": resolution.status,
            "entities": [
                {
                    "entity_id": ref.entity_id,
                    "name": ref.name,
                    "area_id": ref.area_id,
                    "domain": ref.domain,
                }
                for ref in resolution.entities
            ],
            "reason": resolution.reason,
        }
        actions = self._build_actions(intent, resolution.entities)
        payload["actions"] = actions
        if actions:
            guard = await self._guard.check(
                actions, member=member, source="chat_fast_path"
            )
            payload["guard"] = {
                "passed": guard.passed,
                "requires_confirm": guard.requires_confirm,
                "risk_level": guard.risk_level,
                "reasons": guard.reasons,
            }
        payload["route"] = (
            "fast_path" if resolution.status == "ok" or voice_hit else "slow_path"
        )
        return payload

    # ---------- 定时 ----------

    async def _handle_schedule(
        self,
        member,
        spec: ScheduleSpec,
        states: dict[str, dict],
        resolver: EntityResolver,
        view_patterns: list[str] | None,
        settings: dict[str, Any],
        key: tuple[int, int],
        *,
        channel: str = "",
        channel_ref: str = "",
    ) -> RouteResult | None:
        """快路径命中定时：把剥离时间后的动作解析为规范化动作落库，由调度器到时触发。"""
        if not self._allow_rate(member.id, int(settings["member_rate_limit_per_minute"])):
            self._counters["rate_limited"] += 1
            return RouteResult(status="denied", reply="操作太频繁了，请稍后再试。")

        actions = await self._resolve_schedule_actions(
            spec, states, resolver, view_patterns, member
        )
        if not actions:
            self._fallback_reasons["schedule_no_action"] += 1
            logger.warning(
                "fast_path.schedule_no_action",
                text=spec.title[:80],
                action_text=spec.action_text,
            )
            return None

        # 定时动作无人工确认：命中即是「定时预约」，先过一遍安全策略，高风险直接不放行预约
        guard = await self._guard.check(
            actions, member=member, source="scheduled_task"
        )
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            await self._audit(
                member,
                "scheduled_task.denied",
                target=spec.title,
                detail={"reasons": guard.reasons, "actions": actions},
            )
            self._counters["denied"] += 1
            return RouteResult(
                status="denied",
                reply=f"该定时被安全策略拦截：{reason}。可以换一个说法试试。",
            )
        if guard.requires_confirm:
            await self._audit(
                member,
                "scheduled_task.denied",
                target=spec.title,
                detail={"reasons": guard.reasons, "actions": actions},
            )
            return RouteResult(
                status="denied",
                reply="该动作需要人工确认，暂不能设为定时。",
            )

        if spec.kind == "duration":
            # 时长语义：立即执行动作 + 落一条到时自动关
            intent = ParsedIntent(kind="control", original=spec.title)
            executed = await self._execute(
                member, intent, actions, key, reply=f"已开始，{spec.summary}。"
            )
            if executed.status != "executed":
                return executed
            inverse = self._inverse_actions(actions)
            if inverse:
                await self._persist_scheduled_task(
                    member, spec, inverse, status_note="发起时已立即执行",
                    channel=channel, channel_ref=channel_ref,
                )
            return executed

        task_id = await self._persist_scheduled_task(
            member, spec, actions, channel=channel, channel_ref=channel_ref,
        )
        self._counters["scheduled"] += 1
        return RouteResult(
            status="executed",
            reply=self._schedule_reply(spec, task_id),
            actions=[],
        )

    async def _resolve_schedule_actions(
        self,
        spec: ScheduleSpec,
        states: dict[str, dict],
        resolver: EntityResolver,
        view_patterns: list[str] | None,
        member,
    ) -> list[dict[str, Any]]:
        """把剥离时间后的动作文本解析为规范化动作。先快路径，再回退语音解析。"""
        intent = parse_intent(spec.action_text)
        if intent is not None and intent.service:
            if intent.kind == "control":
                resolution = resolver.resolve(
                    intent.target,
                    intent.domain,
                    states,
                    explicit_all=intent.explicit_all,
                    member=member,
                    view_patterns=view_patterns,
                    want_control=True,
                )
                entities = self._filter_already_satisfied(
                    intent, resolution.entities, states, settings=self._settings()
                ) if resolution.status == "ok" else resolution.entities
                if resolution.status == "ok" and entities:
                    return self._build_actions(intent, entities)
        if self._voice is not None and self._settings().get("voice_enabled", True):
            try:
                vr = self._voice.parse(
                    spec.action_text,
                    states=states,
                    resolver=resolver,
                    member=member,
                    view_patterns=view_patterns,
                    settings=self._settings(),
                )
            except Exception:  # noqa: BLE001 - 语音解析失败即放弃定时化
                logger.exception("fast_path.schedule_voice_failed")
                vr = None
            if vr is not None and vr.actions:
                return vr.actions
        return []

    def _inverse_actions(
        self, actions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """同一实体的开/关反转，用于时长定时到时自动回切。取其 domain/service 反转。"""
        inverse = []
        for action in actions:
            service = str(action["service"])
            domain = str(action["domain"])
            flipped = (
                {"turn_on": "turn_off", "open_cover": "close_cover", "unlock": "lock"}.get(
                    service
                )
            )
            if flipped:
                inverse.append(
                    {
                        "domain": domain,
                        "service": flipped,
                        "entity_id": action.get("entity_id"),
                        "data": dict(action.get("data") or {}),
                    }
                )
        return inverse

    async def _persist_scheduled_task(
        self,
        member,
        spec: ScheduleSpec,
        actions: list[dict[str, Any]],
        *,
        status_note: str = "",
        channel: str = "",
        channel_ref: str = "",
    ) -> int:
        summary = spec.summary
        run_at = spec.run_at or spec.auto_run_at
        if status_note:
            summary = f"{summary}（{status_note}）"
        async with self._session_factory() as session:
            task = ScheduledTask(
                member_id=member.id,
                kind=spec.kind,
                text=spec.title,
                summary=summary or spec.title,
                actions_json=json.dumps(actions, ensure_ascii=False),
                source_channel=channel or "",
                source_ref=channel_ref or "",
                run_at=run_at,
                status="pending",
            )
            session.add(task)
            await session.commit()
            task_id = task.id
        await self._audit(
            member,
            "scheduled_task.create",
            target=spec.title,
            detail={"task_id": task_id, "kind": spec.kind, "summary": summary},
        )
        return task_id

    def _schedule_reply(self, spec: ScheduleSpec, task_id: int) -> str:
        if spec.kind == "relative":
            return f"已设置定时：{spec.summary}后{spec.action_text or '执行'}。"
        return f"已设置定时：{spec.summary} {spec.action_text or '执行'}。"

    # ---------- 控制 ----------

    async def _control(
        self,
        member,
        intent: ParsedIntent,
        states: dict[str, dict],
        resolver: EntityResolver,
        view_patterns: list[str] | None,
        settings: dict[str, Any],
        key: tuple[int, int],
        preselected: list[EntityRef] | None = None,
    ) -> RouteResult | None:
        if not self._allow_rate(member.id, int(settings["member_rate_limit_per_minute"])):
            self._counters["rate_limited"] += 1
            return RouteResult(
                status="denied",
                reply="操作太频繁了，请稍后再试。",
            )

        if intent.domain is None and not intent.target:
            last = self._last.get(key)
            if last is not None and last["expires"] >= time.monotonic():
                intent.domain = last["intent"].domain
                intent.service = last["intent"].service

        service = intent.service
        if service is None and intent.domain is not None and intent.op in ("on", "off"):
            from app.fastpath.grammar import service_for

            _, service = service_for(intent.op, intent.domain)
            intent.service = service
        if not service:
            self._fallback_reasons["no_service"] += 1
            return None

        entities = preselected
        if entities is None:
            resolution = resolver.resolve(
                intent.target,
                intent.domain,
                states,
                explicit_all=intent.explicit_all,
                member=member,
                view_patterns=view_patterns,
                want_control=True,
            )
            if resolution.status == "none":
                self._fallback_reasons[resolution.reason or "no_entity"] += 1
                return None
            if resolution.status == "ambiguous":
                return self._clarify(member, intent, resolution.entities, settings, key)
            entities = resolution.entities

        executable = self._filter_already_satisfied(intent, entities, states, settings)
        if not executable:
            names = "、".join(ref.name for ref in entities[:3])
            suffix = "等设备" if len(entities) > 3 else ""
            return RouteResult(
                status="executed",
                reply=f"{names}{suffix}本来就是{self._target_label(intent)}。",
                actions=[],
            )

        actions = self._build_actions(intent, executable)
        guard = await self._guard.check(actions, member=member, source="chat_fast_path")
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            await self._audit(
                member,
                "fast_path.denied",
                target=intent.target or intent.domain or "",
                detail={"reasons": guard.reasons, "actions": actions},
            )
            self._counters["denied"] += 1
            return RouteResult(
                status="denied",
                reply=f"该操作被安全策略拦截：{reason}",
                actions=actions,
            )

        if guard.requires_confirm:
            return await self._proposal(member, intent, guard)

        return await self._execute(member, intent, guard.actions, key, executable)

    async def _handle_voice(
        self,
        member,
        result: VoiceResult,
        states: dict[str, dict],
        settings: dict[str, Any],
        key: tuple[int, int],
    ) -> RouteResult:
        if result.status == "clarify":
            expires = time.monotonic() + int(
                settings.get("session_entity_ttl_seconds", 120)
            )
            if result.slots.function_options:
                self._pending[key] = {
                    "kind": "voice_function",
                    "slots": result.slots,
                    "reply": result.reply,
                    "candidates": [],
                    "expires": expires,
                }
                self._counters["voice_function_clarify"] += 1
            else:
                self._pending[key] = {
                    "kind": "voice",
                    "slots": result.slots,
                    "candidates": result.candidates,
                    "expires": expires,
                }
            self._counters["voice_clarify"] += 1
            return RouteResult(
                status="clarify",
                reply=result.reply,
                candidates=[
                    {
                        "entity_id": ref.entity_id,
                        "name": ref.name,
                        "area_id": ref.area_id,
                    }
                    for ref in result.candidates
                ],
            )
        if result.status == "reject":
            self._counters["voice_rejected"] += 1
            await self._audit(
                member,
                "fast_path.voice_rejected",
                target=result.slots.original,
                detail={
                    "reply": result.reply,
                    "slots": result.slots.as_dict(),
                    "targets": [item.name for item in result.targets],
                },
            )
            return RouteResult(status="rejected", reply=result.reply)
        if result.status == "query":
            self._counters["voice_query"] += 1
            return RouteResult(status="executed", reply=result.reply)
        if result.status != "ok":
            return RouteResult(
                status="error", reply=result.reply or "语音指令无法执行。"
            )
        if not result.actions:
            return RouteResult(status="executed", reply=result.reply)
        return await self._run_voice_actions(member, result, states, settings, key)

    async def _run_voice_actions(
        self,
        member,
        result: VoiceResult,
        states: dict[str, dict],
        settings: dict[str, Any],
        key: tuple[int, int],
    ) -> RouteResult:
        if not self._allow_rate(member.id, int(settings["member_rate_limit_per_minute"])):
            self._counters["rate_limited"] += 1
            return RouteResult(status="denied", reply="操作太频繁了，请稍后再试。")

        if result.slots.op == "state" and self._all_voice_satisfied(
            result, states, settings
        ):
            names = "、".join(item.name for item in result.targets[:3])
            suffix = "等设备" if len(result.targets) > 3 else ""
            return RouteResult(
                status="executed", reply=f"{names}{suffix}本来就是这个状态。"
            )

        guard = await self._guard.check(
            result.actions, member=member, source="chat_fast_path"
        )
        intent = ParsedIntent(kind="control", original=result.slots.original)
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            await self._audit(
                member,
                "fast_path.denied",
                target=result.slots.original,
                detail={"reasons": guard.reasons, "actions": result.actions},
            )
            self._counters["denied"] += 1
            return RouteResult(
                status="denied",
                reply=f"该操作被安全策略拦截：{reason}",
                actions=result.actions,
            )
        if guard.requires_confirm:
            return await self._proposal(member, intent, guard)

        entities = self._voice_refs(result, guard.actions)
        executed = await self._execute(
            member,
            intent,
            guard.actions,
            key,
            entities,
            reply=result.reply,
        )
        if executed.status == "executed":
            self._counters["voice_hit"] += 1
            if result.group:
                self._counters["voice_group_actions"] += len(guard.actions)
            self._remember_voice(key, result, guard.actions)
        return executed

    async def _replay_voice(
        self,
        member,
        last: dict[str, Any],
        states: dict[str, dict],
        settings: dict[str, Any],
        key: tuple[int, int],
    ) -> RouteResult:
        if not self._allow_rate(member.id, int(settings["member_rate_limit_per_minute"])):
            self._counters["rate_limited"] += 1
            return RouteResult(status="denied", reply="操作太频繁了，请稍后再试。")
        result: VoiceResult = last["result"]
        actions = last["actions"]
        guard = await self._guard.check(
            actions, member=member, source="chat_fast_path"
        )
        intent = ParsedIntent(kind="control", original=result.slots.original)
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            self._counters["denied"] += 1
            return RouteResult(
                status="denied",
                reply=f"该操作被安全策略拦截：{reason}",
                actions=actions,
            )
        if guard.requires_confirm:
            return await self._proposal(member, intent, guard)
        executed = await self._execute(
            member,
            intent,
            guard.actions,
            key,
            self._voice_refs(result, guard.actions),
            reply=last["reply"],
        )
        if executed.status == "executed":
            self._counters["voice_hit"] += 1
            self._remember_voice(key, result, guard.actions)
        return executed

    def _all_voice_satisfied(
        self,
        result: VoiceResult,
        states: dict[str, dict],
        settings: dict[str, Any],
    ) -> bool:
        max_age = int(settings.get("state_max_age_seconds", 60))
        for action in result.actions:
            service = str(action.get("service") or "")
            if service not in _OFF_SERVICES | _ON_SERVICES:
                return False
            state = states.get(str(action.get("entity_id") or ""))
            if state is None or not self._fresh(state, max_age):
                return False
            current = str(state.get("state"))
            achieved = (
                current in ("off", "closed")
                if service in _OFF_SERVICES
                else current in ("on", "open")
            )
            if not achieved:
                return False
        return True

    @staticmethod
    def _voice_refs(
        result: VoiceResult, actions: list[dict[str, Any]]
    ) -> list[EntityRef]:
        wanted = {str(action.get("entity_id")) for action in actions}
        return [
            EntityRef(
                entity_id=target.entity_id,
                name=target.name,
                area_id=target.area_id,
                domain=target.domain,
                can_control=True,
            )
            for target in result.targets
            if target.entity_id in wanted
        ]

    def _remember_voice(
        self, key: tuple[int, int], result: VoiceResult, actions: list[dict[str, Any]]
    ) -> None:
        ttl = int(self._settings().get("session_entity_ttl_seconds", 120))
        self._last_voice[key] = {
            "result": result,
            "actions": actions,
            "reply": result.reply,
            "expires": time.monotonic() + ttl,
        }

    async def _execute(
        self,
        member,
        intent: ParsedIntent,
        actions: list[dict[str, Any]],
        key: tuple[int, int],
        entities: list[EntityRef] | None = None,
        reply: str | None = None,
    ) -> RouteResult:
        results: list[dict[str, Any]] = []
        succeeded = 0
        failure = ""

        # 同一服务、同一参数的多个实体合并成一次 HA 调用，减少往返
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for action in actions:
            data = action.get("data") or {}
            group_key = (
                str(action["domain"]),
                str(action["service"]),
                json.dumps(data, sort_keys=True, ensure_ascii=False),
            )
            group = groups.setdefault(
                group_key,
                {
                    "domain": action["domain"],
                    "service": action["service"],
                    "data": data,
                    "entity_ids": [],
                    "actions": [],
                },
            )
            group["entity_ids"].append(action.get("entity_id"))
            group["actions"].append(action)

        for group in groups.values():
            payload = dict(group["data"])
            entity_ids = [entity_id for entity_id in group["entity_ids"] if entity_id]
            if len(entity_ids) == 1:
                payload["entity_id"] = entity_ids[0]
            elif entity_ids:
                payload["entity_id"] = entity_ids
            try:
                output = await self._adapter.call_service(
                    group["domain"], group["service"], payload
                )
                succeeded += len(group["actions"])
                results.extend(
                    {
                        "tool": "call_service",
                        "args": action,
                        "result": {"ok": True, "result": output},
                    }
                    for action in group["actions"]
                )
            except Exception as exc:  # noqa: BLE001 - 如实返回，不自动重试
                failure = str(exc)[:200]
                results.extend(
                    {
                        "tool": "call_service",
                        "args": action,
                        "result": {"ok": False, "error": failure},
                    }
                    for action in group["actions"]
                )
        await self._audit(
            member,
            "fast_path.execute",
            target=actions[0].get("entity_id") if actions else "",
            detail={
                "route": "fast_path",
                "intent": intent.original,
                "actions": actions,
                "succeeded": succeeded,
                "failed": len(actions) - succeeded,
            },
        )
        if succeeded < len(actions):
            self._counters["error"] += 1
            return RouteResult(
                status="error",
                reply=f"操作没有全部成功：{failure or '设备未响应'}。请检查设备连接后重试。",
                actions=actions,
                tool_results=results,
            )
        self._counters["executed"] += 1
        if entities is None:
            entities = [
                EntityRef(
                    entity_id=str(action["entity_id"]),
                    name=self._name_of(str(action["entity_id"])),
                    area_id=None,
                    domain=str(action["domain"]),
                    can_control=True,
                )
                for action in actions
                if action.get("entity_id")
            ]
        self._remember(key, intent, entities)
        return RouteResult(
            status="executed",
            reply=reply or self._success_reply(intent, actions, entities),
            actions=actions,
            tool_results=results,
        )

    async def _proposal(self, member, intent: ParsedIntent, guard) -> RouteResult:
        first = guard.actions[0] if guard.actions else {}
        entity_id = str(first.get("entity_id") or intent.domain or "fast_path")
        record, outcome = await self._proposals.create(
            entity_id=entity_id,
            event_type="chat",
            actions=guard.actions,
            thought=f"快路径请求控制 {intent.target or intent.domain or intent.original or ''}".strip(),
            suggestion=(
                f"快路径识别到 {len(guard.actions)} 个动作，风险等级 {guard.risk_level}，"
                "确认后才会执行"
            ),
            risk_level=guard.risk_level,
            risk_source="code",
            trace_id=uuid.uuid4().hex,
            member_id=member.id,
            source="chat_fast_path",
        )
        self._counters["proposal"] += 1
        if record is None:
            return RouteResult(
                status="proposal",
                reply="相同操作已在待确认中或刚被处理，请到提案页查看。",
                actions=guard.actions,
            )
        await self._publish("proposal.created", record)
        return RouteResult(
            status="proposal",
            reply=(
                f"该操作风险较高，已转为待确认提案 #{record['id']}，"
                "确认后才会执行。"
            ),
            actions=guard.actions,
            proposal=record,
        )

    def _clarify(
        self,
        member,
        intent: ParsedIntent,
        candidates: list[EntityRef],
        settings: dict[str, Any],
        key: tuple[int, int],
    ) -> RouteResult:
        limit = max(1, int(settings.get("max_candidates", 3)))
        options = candidates[:limit]
        lines = [
            f"{index + 1} {ref.name}"
            for index, ref in enumerate(options)
        ]
        self._pending[key] = {
            "kind": "entity",
            "intent": intent,
            "candidates": options,
            "expires": time.monotonic()
            + int(settings.get("session_entity_ttl_seconds", 120)),
        }
        self._counters["clarify"] += 1
        return RouteResult(
            status="clarify",
            reply="找到多个设备：" + "、".join(lines) + "。请回复序号或完整名称。",
            candidates=[
                {
                    "entity_id": ref.entity_id,
                    "name": ref.name,
                    "area_id": ref.area_id,
                }
                for ref in options
            ],
        )

    def _select_candidate(
        self, text: str, candidates: list[EntityRef]
    ) -> EntityRef | None:
        choice = parse_choice(text)
        if choice is not None:
            return candidates[choice] if 0 <= choice < len(candidates) else None
        normalized = normalize(text)
        if not normalized:
            return None
        for ref in candidates:
            if normalized in (normalize(ref.name), normalize(ref.entity_id)):
                return ref
        matched = [ref for ref in candidates if normalized in normalize(ref.name)]
        return matched[0] if len(matched) == 1 else None

    @staticmethod
    def _select_voice_function(text: str, options: list[str]) -> str | None:
        normalized = normalize(text)
        if not normalized:
            return None
        bare = normalized.strip("第号。，,. ")
        if bare.isdigit():
            index = int(bare) - 1
            return options[index] if 0 <= index < len(options) else None
        for name in options:
            label = FUNCTION_LABELS.get(name, name)
            if label and label in normalized:
                return name
        lowered = normalized.lower()
        for name in options:
            if name.lower() in lowered:
                return name
        return None

    # ---------- 查询 ----------

    async def _query(
        self,
        member,
        intent: ParsedIntent,
        states: dict[str, dict],
        resolver: EntityResolver,
        view_patterns: list[str] | None,
    ) -> RouteResult | None:
        if intent.query in ("temperature", "humidity"):
            reply = self._metric_reply(intent, states)
            if reply is None:
                self._fallback_reasons["no_metric"] += 1
                return None
            self._counters["query"] += 1
            return RouteResult(status="executed", reply=reply)

        if intent.query != "state":
            return None
        resolution = resolver.resolve(
            intent.target,
            intent.domain,
            states,
            explicit_all=intent.explicit_all,
            member=member,
            view_patterns=view_patterns,
            want_control=False,
        )
        if resolution.status not in ("ok", "ambiguous") or not resolution.entities:
            self._fallback_reasons["no_query_entity"] += 1
            return None
        refs = resolution.entities
        if len(refs) == 1:
            ref = refs[0]
            state = states.get(ref.entity_id) or {}
            label = _STATE_LABELS.get(str(state.get("state")), str(state.get("state") or "未知"))
            stamp = self._updated_label(state)
            suffix = f"（状态更新于 {stamp}）" if stamp else ""
            reply = f"{ref.name}当前是{label}{suffix}。"
        else:
            on = [
                ref.name
                for ref in refs
                if str((states.get(ref.entity_id) or {}).get("state")) in ("on", "open")
            ]
            unit = UNIT_BY_DOMAIN.get(refs[0].domain, "台设备")
            reply = f"共有 {len(refs)} {unit}，{len(on)} 个开着。"
            if on:
                reply = f"共有 {len(refs)} {unit}，{len(on)} 个开着：" + "、".join(on[:5]) + "。"
        self._counters["query"] += 1
        return RouteResult(status="executed", reply=reply)

    def _metric_reply(self, intent: ParsedIntent, states: dict[str, dict]) -> str | None:
        candidates = self._metric_candidates(intent, states)
        if not candidates:
            return None
        entity_id, number, area_id = candidates[0]
        place = self._area_name(area_id) if area_id else "家里"
        unit = "度" if intent.query == "temperature" else "%"
        return f"{place}当前{('温度' if intent.query == 'temperature' else '湿度')}是 {number:g} {unit}。"

    def _metric_candidates(
        self, intent: ParsedIntent, states: dict[str, dict]
    ) -> list[tuple[str, float, str | None]]:
        target = normalize(intent.target)
        metric = intent.query
        candidates: list[tuple[str, float, str | None]] = []
        for entity_id, state in states.items():
            attributes = (state or {}).get("attributes") or {}
            value = None
            if metric == "temperature":
                if attributes.get("device_class") == "temperature" and state.get("state") not in (
                    "unknown",
                    "unavailable",
                ):
                    value = state.get("state")
                elif entity_id.startswith("climate.") and attributes.get("current_temperature") is not None:
                    value = attributes.get("current_temperature")
            else:
                if attributes.get("device_class") == "humidity" and state.get("state") not in (
                    "unknown",
                    "unavailable",
                ):
                    value = state.get("state")
                elif entity_id.startswith("climate.") and attributes.get("current_humidity") is not None:
                    value = attributes.get("current_humidity")
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            area_id = self._area_of(entity_id)
            candidates.append((entity_id, number, area_id))
        if target and target not in {"家", "家里", "全屋", "全宅", "整个家"}:
            filtered = []
            for entity_id, number, area_id in candidates:
                area_name = self._area_name(area_id) if area_id else ""
                if target in normalize(area_name or "") or target == normalize(area_id or ""):
                    filtered.append((entity_id, number, area_id))
            if not filtered:
                return []
            candidates = filtered
        return candidates

    # ---------- 场景 ----------

    def _scene_by_name(self, name: str) -> SceneRule | None:
        target = normalize(name)
        for rule in self._rules:
            if normalize(rule.name) == target:
                return rule
        return None

    async def _scene(
        self,
        member,
        rule: SceneRule,
        states: dict[str, dict],
        resolver: EntityResolver,
        view_patterns: list[str] | None,
        settings: dict[str, Any],
    ) -> RouteResult:
        if not self._allow_rate(member.id, int(settings["member_rate_limit_per_minute"])):
            self._counters["rate_limited"] += 1
            return RouteResult(status="denied", reply="操作太频繁了，请稍后再试。")

        actions: list[dict[str, Any]] = []
        for template in rule.actions:
            expanded = self._expand_action(template, states, resolver, view_patterns)
            if expanded is None:
                self._counters["error"] += 1
                return RouteResult(
                    status="error",
                    rule_id=rule.id,
                    reply=f"场景「{rule.name}」中有设备未找到，未执行任何动作。",
                )
            actions.extend(expanded)
        if not actions:
            self._counters["error"] += 1
            return RouteResult(
                status="error",
                rule_id=rule.id,
                reply=f"场景「{rule.name}」没有可执行的设备。",
            )

        guard = await self._guard.check(actions, member=member, source="chat_fast_path")
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            await self._audit(
                member,
                "fast_path.denied",
                target=rule.name,
                detail={"rule_id": rule.id, "reasons": guard.reasons},
            )
            self._counters["denied"] += 1
            return RouteResult(
                status="denied",
                rule_id=rule.id,
                reply=f"场景「{rule.name}」被安全策略拦截：{reason}",
            )
        if guard.requires_confirm:
            result = await self._proposal(
                member,
                ParsedIntent(kind="control", original=rule.name),
                guard,
            )
            result.rule_id = rule.id
            return result

        result = await self._execute(
            member,
            ParsedIntent(kind="control", original=rule.name),
            guard.actions,
            (member.id, 0),
        )
        result.rule_id = rule.id
        if result.status == "executed":
            template = rule.reply_template or f"已执行场景「{rule.name}」。"
            try:
                result.reply = template.format(count=len(guard.actions), name=rule.name)
            except (KeyError, IndexError, ValueError):
                result.reply = f"已执行场景「{rule.name}」。"
            self._rule_hits[f"scene:{rule.id}"] += 1
        await self._audit(
            member,
            "fast_path.scene",
            target=rule.name,
            detail={
                "rule_id": rule.id,
                "version": rule.version,
                "actions": actions,
                "status": result.status,
            },
        )
        return result

    # ---------- 情感类主观感受兜底 ----------

    async def _emotional(
        self,
        member,
        feeling: EmotionalFeeling,
        actions: list[dict[str, Any]],
        states: dict[str, dict],
        settings: dict[str, Any],
        key: tuple[int, int],
    ) -> RouteResult | None:
        """按情感词映射出的动作执行；走与场景一致的安全策略与执行流程。"""
        if not self._allow_rate(member.id, int(settings["member_rate_limit_per_minute"])):
            self._counters["rate_limited"] += 1
            return RouteResult(status="denied", reply="操作太频繁了，请稍后再试。")

        guard = await self._guard.check(actions, member=member, source="chat_fast_path")
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            await self._audit(
                member,
                "fast_path.denied",
                target=feeling.matched_word,
                detail={"kind": feeling.kind, "reasons": guard.reasons},
            )
            self._counters["denied"] += 1
            return RouteResult(
                status="denied",
                reply=f"针对「{feeling.matched_word}」的调整被安全策略拦截：{reason}",
            )

        intent = ParsedIntent(kind="control", original=feeling.matched_word)
        if guard.requires_confirm:
            return await self._proposal(member, intent, guard)

        result = await self._execute(
            member, intent, guard.actions, key, reply=self._emotional_reply(feeling, guard.actions)
        )
        if result.status == "executed":
            self._counters["emotional_executed"] += 1
        return result

    def _emotional_reply(
        self, feeling: EmotionalFeeling, actions: list[dict[str, Any]]
    ) -> str:
        """按感受给出一句贴合依据的完成语，避免复用通用"已打开/已关闭"。"""
        action = actions[0] if actions else {}
        service = action.get("service")
        data = action.get("data") or {}
        name = self._name_of(str(action.get("entity_id") or ""))

        if service == "set_temperature" and data.get("temperature") is not None:
            return f"觉得{feeling.matched_word}，已把{name}调到 {data['temperature']:g} 度。"
        if service == "set_hvac_mode":
            mode_label = {
                "heat": "制热", "cool": "制冷", "dry": "除湿", "fan_only": "换气",
            }.get(data.get("hvac_mode"), "对应模式")
            return f"觉得{feeling.matched_word}，已把{name}切到{mode_label}。"

        verb = "打开"
        if service in _OFF_SERVICES:
            verb = "关闭"
        elif service in _ON_SERVICES:
            verb = "打开"
        return f"针对「{feeling.matched_word}」，已{verb} {name}。"

    def _expand_action(
        self,
        template: dict[str, Any],
        states: dict[str, dict],
        resolver: EntityResolver,
        view_patterns: list[str] | None,
    ) -> list[dict[str, Any]] | None:
        domain = str(template.get("domain") or "").strip()
        service = str(template.get("service") or "").strip()
        if not domain or not service:
            return None
        data = dict(template.get("data") or {})
        target = template.get("target") or {"type": "domain_all", "domain": domain}
        target_type = target.get("type")

        if target_type == "entity":
            entity_id = str(target.get("entity_id") or "")
            if not entity_id:
                return None
            return [
                {"domain": domain, "service": service, "entity_id": entity_id, "data": data}
            ]
        if target_type == "domain_all":
            resolution = resolver.resolve(
                "",
                domain,
                states,
                explicit_all=True,
                view_patterns=view_patterns,
                want_control=True,
            )
            if not resolution.entities:
                return None
            return [
                {
                    "domain": domain,
                    "service": service,
                    "entity_id": ref.entity_id,
                    "data": data,
                }
                for ref in resolution.entities
            ]
        if target_type == "area":
            area_text = str(target.get("area") or "")
            resolution = resolver.resolve_in_area(
                area_text,
                domain,
                states,
                view_patterns=view_patterns,
                want_control=True,
            )
            if not resolution.entities:
                return None
            return [
                {
                    "domain": domain,
                    "service": service,
                    "entity_id": ref.entity_id,
                    "data": data,
                }
                for ref in resolution.entities
            ]
        return None

    # ---------- 工具 ----------

    def _build_actions(
        self, intent: ParsedIntent, entities: list[EntityRef]
    ) -> list[dict[str, Any]]:
        if not intent.service:
            return []
        data = dict(intent.data or {})
        return [
            {
                "domain": ref.domain,
                "service": intent.service,
                "entity_id": ref.entity_id,
                "data": dict(data),
            }
            for ref in entities
        ]

    def _filter_already_satisfied(
        self,
        intent: ParsedIntent,
        entities: list[EntityRef],
        states: dict[str, dict],
        settings: dict[str, Any],
    ) -> list[EntityRef]:
        if intent.service not in _OFF_SERVICES | _ON_SERVICES:
            return entities
        max_age = int(settings.get("state_max_age_seconds", 60))
        desired = "off" if intent.service in _OFF_SERVICES else "on"
        result = []
        for ref in entities:
            state = states.get(ref.entity_id)
            if state is None or not self._fresh(state, max_age):
                result.append(ref)
                continue
            current = str(state.get("state"))
            achieved = (
                current in ("off", "closed")
                if desired == "off"
                else current in ("on", "open")
            )
            if not achieved:
                result.append(ref)
        return result

    def _success_reply(
        self,
        intent: ParsedIntent,
        actions: list[dict[str, Any]],
        entities: list[EntityRef] | None = None,
    ) -> str:
        verb = "关闭" if intent.service in _OFF_SERVICES else "打开"
        if intent.service == "set_temperature" and intent.data.get("temperature") is not None:
            verb = f"调到 {intent.data['temperature']:g} 度"
        elif intent.service == "set_hvac_mode":
            verb = "切换模式"
        elif intent.service == "set_brightness":
            verb = f"调到 {intent.data.get('brightness_pct')}%"
        if len(actions) == 1:
            if entities:
                name = entities[0].name
            else:
                name = self._name_of(str(actions[0].get("entity_id") or ""))
            return f"已{verb}{name}。"
        domain = str(actions[0].get("domain") or "")
        unit = UNIT_BY_DOMAIN.get(domain, "台设备")
        return f"已{verb} {len(actions)} {unit}。"

    def _name_of(self, entity_id: str) -> str:
        for alias in self._aliases:
            if alias.entity_id == entity_id:
                return alias.display_name or alias.alias
        return entity_id

    def _friendly(self, entity_id: str, states: dict[str, dict]) -> str:
        attributes = (states.get(entity_id) or {}).get("attributes") or {}
        return (
            attributes.get("friendly_name")
            or self._name_of(entity_id)
        )

    def _target_label(self, intent: ParsedIntent) -> str:
        if intent.service in _OFF_SERVICES:
            return "关着的"
        if intent.service in _ON_SERVICES:
            return "开着的"
        return "满足条件的"

    def _remember(
        self, key: tuple[int, int], intent: ParsedIntent, entities: list[EntityRef]
    ) -> None:
        ttl = int(self._settings().get("session_entity_ttl_seconds", 120))
        self._last[key] = {
            "intent": intent,
            "entities": entities,
            "expires": time.monotonic() + ttl,
        }

    def _allow_rate(self, member_id: int, limit: int) -> bool:
        now = time.monotonic()
        bucket = self._rate.setdefault(member_id, deque())
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    async def _states(self) -> dict[str, dict]:
        cache = getattr(self._adapter, "cache", None)
        if cache is None:
            return {}
        try:
            return await cache.snapshot()
        except Exception:  # noqa: BLE001 - 缓存异常按空状态处理，后续走慢路径
            logger.exception("fast_path.state_snapshot_failed")
            return {}

    async def _view_patterns(self, member) -> list[str] | None:
        if member.role == "admin":
            return None
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(MemberEntityPermission).where(
                        MemberEntityPermission.member_id == member.id,
                        MemberEntityPermission.can_view.is_(True),
                    )
                )
            ).all()
        return [row.entity_pattern for row in rows]

    def _area_of(self, entity_id: str) -> str | None:
        registry = getattr(self._adapter, "registry", None)
        area_of = getattr(registry, "area_of", None)
        return area_of(entity_id) if callable(area_of) else None

    def _area_name(self, area_id: str | None) -> str | None:
        if not area_id:
            return None
        registry = getattr(self._adapter, "registry", None)
        area_name = getattr(registry, "area_name", None)
        return area_name(area_id) if callable(area_name) else None

    @staticmethod
    def _fresh(state: dict, max_age: int) -> bool:
        raw = state.get("last_updated") or state.get("last_changed")
        if not raw:
            return True
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - moment).total_seconds()
        return 0 <= age <= max_age

    @staticmethod
    def _updated_label(state: dict) -> str:
        raw = state.get("last_updated") or state.get("last_changed")
        if not raw:
            return ""
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%H:%M:%S")

    def _finish(self, result: RouteResult, started: float) -> RouteResult:
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        self._latencies.append(result.latency_ms)
        if result.rule_id is not None:
            self._rule_hits[f"scene:{result.rule_id}"] += 1
        return result

    async def _audit(
        self, member, action: str, *, target: str, detail: dict[str, Any]
    ) -> None:
        try:
            async with self._session_factory() as session:
                add_audit(
                    session,
                    member_id=member.id,
                    action=action,
                    target=str(target)[:255],
                    detail=detail,
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计失败不影响执行结果返回
            logger.exception("fast_path.audit_failed", action=action)

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._sse is None:
            return
        try:
            await self._sse.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - 推送失败不影响执行
            logger.exception("fast_path.sse_failed")

    def stats(self) -> dict[str, Any]:
        latencies = sorted(self._latencies)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0
        return {
            "enabled": self.enabled,
            "counters": dict(self._counters),
            "requests": sum(self._counters.values()),
            "latency_ms": {"p50": p50, "p95": p95, "samples": len(latencies)},
            "rule_hits": dict(self._rule_hits.most_common(20)),
            "fallback_reasons": dict(self._fallback_reasons.most_common(20)),
            "rules_loaded": len(self._rules),
            "aliases_loaded": len(self._aliases),
            "voice_loaded": self._voice is not None,
            "voice_entries": self._voice_entries,
        }

    async def voice_directory(self) -> dict[str, Any]:
        registry = getattr(self._adapter, "registry", None)
        states = await self._states()
        floors = [
            {"id": floor_id, "name": item.get("name") or floor_id}
            for floor_id, item in (getattr(registry, "floors", {}) or {}).items()
        ]
        areas = [
            {
                "id": area_id,
                "name": item.get("name") or area_id,
                "floor_id": item.get("floor_id") or "",
            }
            for area_id, item in (getattr(registry, "areas", {}) or {}).items()
        ]
        floor_of = getattr(registry, "floor_of", None)
        area_of = getattr(registry, "area_of", None)
        aliases = {record.entity_id: record for record in self._aliases}

        def entry(entity_id: str, name: str, area_id: str, device_type: str) -> dict[str, Any]:
            return {
                "entity_id": entity_id,
                "name": name,
                "device_type": device_type,
                "area_id": area_id or "",
                "floor_id": (floor_of(entity_id) if callable(floor_of) else "") or "",
            }

        entities: dict[str, dict[str, Any]] = {}
        for entity_id, state in states.items():
            capability = build_capability(entity_id, state or {})
            if not capability.device_type or not capability.controllable:
                continue
            record = aliases.get(entity_id)
            name = (record.display_name or record.alias) if record else capability.name
            area_id = (
                (record.area_id if record else "")
                or (area_of(entity_id) if callable(area_of) else "")
                or capability.area_id
                or ""
            )
            entities[entity_id] = entry(entity_id, name, area_id, capability.device_type)
        for record in self._aliases:
            if record.entity_id in entities:
                continue
            name = record.display_name or record.alias
            capability = build_capability(
                record.entity_id,
                states.get(record.entity_id) or {},
                name=name,
                area_id=record.area_id,
            )
            entities[record.entity_id] = entry(
                record.entity_id,
                name,
                record.area_id
                or (area_of(record.entity_id) if callable(area_of) else "")
                or "",
                capability.device_type,
            )
        listed = sorted(entities.values(), key=lambda item: (item["area_id"], item["name"]))
        return {"floors": floors, "areas": areas, "entities": listed}
