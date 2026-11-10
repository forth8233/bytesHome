from __future__ import annotations

import asyncio
import fnmatch
import json
import secrets
import time
import uuid
from collections import deque
from datetime import timedelta
from typing import Any

import structlog
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.automation import AutomationValidationError, parse_automation
from app.chat.memory_extract import extract_and_store_memory
from app.chat.prompt import build_messages, tool_results_message, utcnow, wrap_untrusted
from app.chat.schema import ChatDecision
from app.core.logging import scrub_text
from app.db.models import (
    AutomationDraft,
    ChatMessage,
    ChatSession,
    Member,
    MemberEntityPermission,
    MemberPreference,
    ReasoningTrace,
)
from app.llm.dsml import parse_dsml_decision
from app.services.audit import add_audit

logger = structlog.get_logger(__name__)

MAX_ROUNDS = 4
MAX_TOOL_CALLS_PER_ROUND = 3
DRAFT_TTL_SECONDS = 300

# 没有真正执行动作时，模型仍在声称完成的措辞
_COMPLETION_CLAIMS = (
    "已完成",
    "已经完成",
    "已关闭",
    "已经关闭",
    "已打开",
    "已经打开",
    "已开启",
    "已经开启",
    "已执行",
    "已处理",
    "已成功",
    "操作成功",
    "执行成功",
    "已调至",
    "已设为",
    "已设置为",
    "已为你关",
    "已为你开",
    "已为你调",
    "已帮您关",
    "已帮您开",
)
_NO_ACTION_REPLY = "我没有实际执行任何设备操作。请确认要控制的设备名称或实体 ID，我再试一次。"
_LLM_DOWN_REPLY = (
    "大模型暂时不可用，我没有执行任何设备动作。"
    "明确的控制仍可直接说，例如「关灯」「空调调到26度」「客厅温度」。"
)
_LLM_DOWN_PARTIAL_REPLY = (
    "大模型暂时不可用，后续思考中断了。已经执行的设备动作见下方工具调用，"
    "请先确认设备状态；明确的控制仍可直接说，例如「关灯」。"
)
_TIMEOUT_REPLY = (
    "这次思考超时了，后续步骤没有完成。如果刚才涉及设备控制，"
    "请先在 Home Assistant 里确认设备状态，不要盲目重复操作。"
)
_RATE_LIMIT_REPLY = (
    "请求有点频繁，这次没有开始处理。请稍等几秒再试；"
    "明确的控制仍可直接说，例如「关灯」「空调调到26度」。"
)
_INTERNAL_ERROR_REPLY = (
    "这次处理出错了，后续步骤没有完成。如果刚才涉及设备控制，请先确认设备状态。"
)


def _claims_completion(reply: str) -> bool:
    return any(token in reply for token in _COMPLETION_CLAIMS)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


class ChatService:
    """对话工具循环：只读查询直接返回，设备动作全部经过 ActionGuard。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config,
        adapter,
        guard,
        proposals,
        client,
        sse=None,
        fast_path=None,
        timeout_seconds: float = 120.0,
        slow_path_limit_per_minute: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._adapter = adapter
        self._guard = guard
        self._proposals = proposals
        self._client = client
        self._sse = sse
        self._fast_path = fast_path
        self._timeout_seconds = max(0.01, float(timeout_seconds))
        self._slow_limit = max(0, int(slow_path_limit_per_minute))
        self._slow_calls: dict[int, deque[float]] = {}
        self._limit_lock = asyncio.Lock()
        # 群名/来源渠道显示解析器：(channel, ref) -> label | None，可空
        self._channel_label_resolver = None

    def set_channel_label_resolver(self, resolver) -> None:
        """挂接渠道专属解析器（如飞书按 chat_id 取群名）。"""
        self._channel_label_resolver = resolver

    # ---------- 对话 ----------

    async def chat(
        self,
        member: Member,
        message: str,
        session_id: int | None = None,
        *,
        channel: str = "",
        channel_ref: str = "",
    ) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "message is empty")
        started = time.perf_counter()

        async with self._session_factory() as session:
            if session_id is None:
                # 有渠道来源（如飞书）时，优先复用该成员在该渠道会话里已有的会话，
                # 避免每来一条新消息都新建一个对话。
                if channel and channel_ref:
                    chat_session = await session.scalar(
                        select(ChatSession)
                        .where(
                            ChatSession.member_id == member.id,
                            ChatSession.channel == channel,
                            ChatSession.channel_ref == channel_ref,
                        )
                        .order_by(ChatSession.updated_at.desc())
                        .limit(1)
                    )
                else:
                    chat_session = None
                if chat_session is None:
                    chat_session = ChatSession(
                        member_id=member.id,
                        title=message[:30],
                        channel=channel or "",
                        channel_ref=channel_ref or "",
                        updated_at=utcnow(),
                    )
                    session.add(chat_session)
                    await session.flush()
                session_id = chat_session.id
            else:
                chat_session = await session.get(ChatSession, session_id)
                if chat_session is None or chat_session.member_id != member.id:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
            history = list(
                (
                    await session.scalars(
                        select(ChatMessage)
                        .where(ChatMessage.session_id == session_id)
                        .order_by(ChatMessage.id.desc())
                        .limit(10)
                    )
                ).all()
            )[::-1]
            session.add(
                ChatMessage(
                    session_id=session_id,
                    role="user",
                    content=message,
                )
            )
            chat_session.updated_at = utcnow()
            await session.commit()

        try:
            preferences = await self._preferences(member.id)
        except Exception:  # noqa: BLE001 - 偏好加载失败不应阻断对话
            logger.exception("chat.preferences_load_failed", member_id=member.id)
            preferences = {}

        messages = build_messages(history, message, preferences=preferences)
        route_result = None
        if self._fast_path is not None:
            try:
                route_result = await self._fast_path.route(
                    member, message, session_id, channel=channel, channel_ref=channel_ref
                )
            except Exception:  # noqa: BLE001 - 快路径失败必须回退到慢思考
                logger.exception("chat.fast_path_failed", session_id=session_id)
                route_result = None
        if route_result is not None:
            return await self._finish_fast_path(session_id, route_result)

        if not await self._allow_slow_path(member.id):
            logger.warning("chat.slow_path_rate_limited", member_id=member.id)
            return await self._finish_slow_path(
                session_id,
                member=member,
                user_text=message,
                reply=_RATE_LIMIT_REPLY,
                tool_trace=[],
                proposal=None,
                draft=None,
                route_status="rate_limited",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        try:
            async with asyncio.timeout(self._timeout_seconds):
                outcome = await self._run_slow_path(messages, member, session_id)
        except TimeoutError:
            logger.warning(
                "chat.slow_path_timeout",
                session_id=session_id,
                member_id=member.id,
                timeout_seconds=self._timeout_seconds,
            )
            outcome = {
                "reply": _TIMEOUT_REPLY,
                "tool_trace": [],
                "proposal": None,
                "draft": None,
                "route_status": "timeout",
            }
        except Exception:  # noqa: BLE001 - 未预期异常必须如实返回，不能 500
            logger.exception("chat.slow_path_failed", session_id=session_id)
            outcome = {
                "reply": _INTERNAL_ERROR_REPLY,
                "tool_trace": [],
                "proposal": None,
                "draft": None,
                "route_status": "error",
            }
        return await self._finish_slow_path(
            session_id,
            member=member,
            user_text=message,
            latency_ms=int((time.perf_counter() - started) * 1000),
            **outcome,
        )

    async def _preferences(self, member_id: int) -> dict[str, str]:
        """读取该成员沉淀的环境偏好（温度/光线/窗帘/加湿等）与综合场景预设，供慢路径参考。"""
        try:
            async with self._session_factory() as session:
                rows = list(
                    (
                        await session.scalars(
                            select(MemberPreference).where(
                                MemberPreference.member_id == member_id,
                                MemberPreference.key.like("climate.%"),
                            )
                        )
                    ).all()
                )
                for domain in ("light.", "scene."):
                    rows += list(
                        (
                            await session.scalars(
                                select(MemberPreference).where(
                                    MemberPreference.member_id == member_id,
                                    MemberPreference.key.like(f"{domain}%"),
                                )
                            )
                        ).all()
                    )
        except Exception:  # noqa: BLE001
            logger.exception("chat.preferences_query_failed", member_id=member_id)
            return {}
        return {row.key: (row.value_json or row.value or "") for row in rows}

    async def _allow_slow_path(self, member_id: int) -> bool:
        if self._slow_limit <= 0:
            return True
        now = time.monotonic()
        async with self._limit_lock:
            calls = self._slow_calls.setdefault(member_id, deque())
            while calls and now - calls[0] > 60:
                calls.popleft()
            if len(calls) >= self._slow_limit:
                return False
            calls.append(now)
        return True

    async def _run_slow_path(
        self, messages: list[dict[str, Any]], member: Member, session_id: int
    ) -> dict[str, Any]:
        tool_trace: list[dict[str, Any]] = []
        final_reply = ""
        proposal: dict[str, Any] | None = None
        draft: dict[str, Any] | None = None
        action_performed = False
        llm_down = False

        for round_index in range(1, MAX_ROUNDS + 1):
            result = await self._client.structured_with_fallback(
                messages,
                ChatDecision,
                schema_name="chat_decision",
                temperature=0.3,
            )
            await self._write_trace(result, session_id, round_index)

            decision: ChatDecision | None = None
            if isinstance(result.data, dict):
                try:
                    decision = ChatDecision.model_validate(result.data)
                except ValidationError:
                    decision = None
            if decision is None:
                # DeepSeek 部分版本会返回原生 DSML 工具调用标记，按同一语义解析
                decision = parse_dsml_decision(result.content)
            if decision is None:
                if not result.ok:
                    llm_down = True
                    final_reply = (
                        _LLM_DOWN_PARTIAL_REPLY if tool_trace else _LLM_DOWN_REPLY
                    )
                else:
                    final_reply = "抱歉，我没有得到可执行的结果，请换一种说法再试。"
                break

            final_reply = decision.reply.strip()
            if not decision.tool_calls:
                break

            stop = False
            for call in decision.tool_calls[:MAX_TOOL_CALLS_PER_ROUND]:
                outcome = await self._execute_tool(call.tool, call.args, member)
                tool_trace.append(
                    {
                        "tool": call.tool,
                        "args": call.args,
                        "result": outcome["persist"],
                    }
                )
                if outcome.get("proposal") is not None:
                    proposal = outcome["proposal"]
                    action_performed = True
                if outcome.get("draft") is not None:
                    draft = outcome["draft"]
                    action_performed = True
                if call.tool == "call_service" and outcome["persist"].get("ok"):
                    action_performed = True
                if outcome.get("final_reply"):
                    final_reply = outcome["final_reply"]
                if outcome.get("stop"):
                    stop = True
                    break

            if stop:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": decision.reply or "正在执行工具调用。",
                }
            )
            messages.append(
                tool_results_message([item["result"] for item in tool_trace[-MAX_TOOL_CALLS_PER_ROUND:]])
            )
        else:
            final_reply = final_reply or "工具调用次数已达上限，请换一种说法再试。"

        if not final_reply:
            final_reply = _NO_ACTION_REPLY
        elif not action_performed and _claims_completion(final_reply):
            final_reply = _NO_ACTION_REPLY

        if llm_down:
            route_status = "degraded"
        elif proposal is not None:
            route_status = "proposal"
        elif draft is not None:
            route_status = "draft"
        elif action_performed:
            route_status = "executed"
        else:
            route_status = "replied"
        return {
            "reply": final_reply,
            "tool_trace": tool_trace,
            "proposal": proposal,
            "draft": draft,
            "route_status": route_status,
        }

    async def _finish_slow_path(
        self,
        session_id: int,
        *,
        member: Member,
        user_text: str,
        reply: str,
        tool_trace: list[dict[str, Any]],
        proposal: dict[str, Any] | None,
        draft: dict[str, Any] | None,
        route_status: str,
        latency_ms: int,
    ) -> dict[str, Any]:
        route_meta = {
            "route": "slow_path",
            "route_status": route_status,
            "latency_ms": latency_ms,
        }
        async with self._session_factory() as session:
            session.add(
                ChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content=reply,
                    tool_calls_json=json.dumps(
                        {"tool_calls": tool_trace, "route": route_meta},
                        ensure_ascii=False,
                    )[:8000],
                )
            )
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is not None:
                chat_session.updated_at = utcnow()
            await session.commit()

        # 自动记忆通道：成功路径尽力抽取可沉淀的长期偏好，失败静默不影响回复
        if route_status not in {"degraded", "timeout", "error", "rate_limited"}:
            try:
                await extract_and_store_memory(
                    self._client, self._session_factory, member, user_text
                )
            except Exception:  # noqa: BLE001 - 记忆抽取绝不允许污染对话
                logger.exception("chat.memory_extract_schedule_failed")

        return {
            "session_id": session_id,
            "reply": reply,
            "tool_results": tool_trace,
            "proposal": proposal,
            "draft": draft,
            "created_at": _iso(utcnow()),
            "route": "slow_path",
            "route_status": route_status,
            "latency_ms": latency_ms,
            "degraded": route_status in {"degraded", "timeout", "error", "rate_limited"},
        }

    async def _finish_fast_path(self, session_id: int, route_result) -> dict[str, Any]:
        tool_trace = list(route_result.tool_results)
        route_meta = {
            "route": "fast_path",
            "route_status": route_result.status,
            "latency_ms": route_result.latency_ms,
            "rule_id": route_result.rule_id,
        }
        async with self._session_factory() as session:
            session.add(
                ChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content=route_result.reply,
                    tool_calls_json=json.dumps(
                        {"tool_calls": tool_trace, "route": route_meta}, ensure_ascii=False
                    )[:8000],
                )
            )
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is not None:
                chat_session.updated_at = utcnow()
            await session.commit()
        return {
            "session_id": session_id,
            "reply": route_result.reply,
            "tool_results": tool_trace,
            "proposal": route_result.proposal,
            "draft": None,
            "created_at": _iso(utcnow()),
            **route_meta,
            "candidates": route_result.candidates,
        }

    async def _execute_tool(self, tool: str, args: dict[str, Any], member: Member) -> dict[str, Any]:
        if tool == "get_state":
            return await self._tool_get_state(args, member)
        if tool == "call_service":
            return await self._tool_call_service(args, member)
        if tool == "create_automation":
            return await self._tool_create_automation(args, member)
        return {
            "persist": {"ok": False, "error": f"unknown tool: {tool}"},
            "final_reply": f"不支持的工具：{tool}",
            "stop": True,
        }

    async def _tool_get_state(
        self, args: dict[str, Any], member: Member
    ) -> dict[str, Any]:
        patterns = await self._view_patterns(member)
        entity_id = str(args.get("entity_id") or "").strip()
        domain = str(args.get("domain") or "").strip()
        area_id = str(args.get("area_id") or "").strip()
        cache = getattr(self._adapter, "cache", None)
        if cache is None:
            return {"persist": {"ok": False, "error": "state cache is unavailable"}}

        if entity_id:
            if patterns is not None and not any(
                fnmatch.fnmatchcase(entity_id, pattern) for pattern in patterns
            ):
                return {"persist": {"ok": False, "error": "entity not found or not visible"}}
            state = await cache.get(entity_id)
            if state is None:
                return {"persist": {"ok": False, "error": "entity not found"}}
            return {"persist": {"ok": True, "entities": [self._state_item(entity_id, state)]}}

        states = await cache.snapshot()
        items: list[dict[str, Any]] = []
        registry = getattr(self._adapter, "registry", None)
        for eid, state in states.items():
            if domain and not str(eid).startswith(f"{domain}."):
                continue
            if patterns is not None and not any(
                fnmatch.fnmatchcase(str(eid), pattern) for pattern in patterns
            ):
                continue
            if area_id and registry is not None and registry.area_of(str(eid)) != area_id:
                continue
            items.append(self._state_item(str(eid), state))
            if len(items) >= 30:
                break
        return {"persist": {"ok": True, "entities": items, "truncated": len(items) >= 30}}

    def _state_item(self, entity_id: str, state: dict[str, Any]) -> dict[str, Any]:
        attributes = state.get("attributes") or {}
        registry = getattr(self._adapter, "registry", None)
        return {
            "entity_id": entity_id,
            "name": wrap_untrusted(attributes.get("friendly_name") or entity_id),
            "state": wrap_untrusted(state.get("state")),
            "area_id": registry.area_of(entity_id) if registry is not None else None,
        }

    async def _tool_call_service(
        self, args: dict[str, Any], member: Member
    ) -> dict[str, Any]:
        action = {
            "domain": str(args.get("domain") or "").strip(),
            "service": str(args.get("service") or "").strip(),
            "entity_id": args.get("entity_id"),
            "data": dict(args.get("data") or {}),
        }
        guard = await self._guard.check([action], member=member, source="chat")
        if not guard.passed:
            reason = "; ".join(guard.reasons)[:300]
            return {
                "persist": {"ok": False, "denied": True, "error": reason},
                "final_reply": f"该操作被安全策略拦截：{reason}",
                "stop": True,
            }

        if guard.requires_confirm:
            entity_id = action.get("entity_id") or "chat"
            record, outcome = await self._proposals.create(
                entity_id=str(entity_id),
                event_type="chat",
                actions=guard.actions,
                thought=f"用户通过对话请求控制 {entity_id}",
                suggestion=f"对话请求执行 {action['domain']}.{action['service']}，需确认后执行",
                risk_level=guard.risk_level,
                risk_source="code",
                trace_id=uuid.uuid4().hex,
                member_id=member.id,
                source="chat",
            )
            if record is None:
                return {
                    "persist": {
                        "ok": False,
                        "pending_confirmation": True,
                        "error": "same action is already pending or was just handled",
                    },
                    "final_reply": "相同操作已在待确认中或刚被处理，请到提案页查看。",
                    "stop": True,
                }
            await self._publish("proposal.created", record)
            return {
                "persist": {
                    "ok": False,
                    "pending_confirmation": True,
                    "proposal_id": record["id"],
                    "risk_level": record["risk_level"],
                },
                "final_reply": (
                    f"该操作风险较高，已转为待确认提案 #{record['id']}，"
                    "确认后才会执行。"
                ),
                "proposal": record,
                "stop": True,
            }

        payload = dict(action.get("data") or {})
        if action.get("entity_id"):
            payload["entity_id"] = action["entity_id"]
        try:
            output = await self._adapter.call_service(
                action["domain"], action["service"], payload
            )
        except Exception as exc:  # noqa: BLE001 - 失败如实返回，不自动重试
            logger.warning("chat.call_service_failed", entity_id=action.get("entity_id"))
            return {
                "persist": {"ok": False, "error": str(exc)[:300]},
                "final_reply": "设备动作执行失败，请稍后再试或检查 Home Assistant 连接。",
                "stop": True,
            }
        return {"persist": {"ok": True, "result": output}}

    async def _tool_create_automation(
        self, args: dict[str, Any], member: Member
    ) -> dict[str, Any]:
        raw = str(args.get("yaml") or "")
        try:
            config = parse_automation(raw, self._config.whitelist())
        except AutomationValidationError as exc:
            return {
                "persist": {"ok": False, "error": str(exc)[:300]},
                "final_reply": f"自动化草稿未通过安全校验：{exc}",
                "stop": True,
            }

        token = secrets.token_urlsafe(24)
        expires_at = utcnow() + timedelta(seconds=DRAFT_TTL_SECONDS)
        async with self._session_factory() as session:
            draft = AutomationDraft(
                member_id=member.id,
                confirm_token=token,
                yaml_content=raw,
                status="pending",
                expires_at=expires_at,
            )
            session.add(draft)
            await session.commit()
            await session.refresh(draft)
            draft_id = draft.id

        return {
            "persist": {
                "ok": True,
                "draft_id": draft_id,
                "alias": config.get("alias"),
                "expires_at": _iso(expires_at),
                "requires_confirmation": True,
            },
            "draft": {
                "id": draft_id,
                "confirm_token": token,
                "yaml": raw,
                "alias": config.get("alias"),
                "expires_at": _iso(expires_at),
            },
            "final_reply": "已生成自动化草稿，请检查 YAML 并确认后才会写入 Home Assistant。",
            "stop": True,
        }

    async def _view_patterns(self, member: Member) -> list[str] | None:
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

    async def _write_trace(self, result, session_id: int, round_index: int) -> None:
        try:
            async with self._session_factory() as session:
                session.add(
                    ReasoningTrace(
                        trace_id=uuid.uuid4().hex,
                        kind="chat",
                        provider=result.provider,
                        model=result.model,
                        input_summary=json.dumps(
                            {"session_id": session_id, "round": round_index}
                        ),
                        input_tokens=result.prompt_tokens,
                        output_tokens=result.completion_tokens,
                        raw_output=scrub_text(result.content),
                        parsed_ok=result.parsed_ok,
                        duration_ms=result.duration_ms,
                        error=scrub_text(result.error, 1000),
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 审计写失败不影响对话
            logger.exception("chat.trace_failed", session_id=session_id)

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._sse is None:
            return
        try:
            await self._sse.publish(event_type, payload)
        except Exception:  # noqa: BLE001 - 推送失败不影响对话
            logger.exception("chat.sse_failed")

    # ---------- 自动化确认 ----------

    async def confirm_automation(
        self,
        member: Member,
        confirm_token: str,
        *,
        yaml_content: str | None = None,
        ip: str = "",
    ) -> dict[str, Any]:
        now = utcnow()
        async with self._session_factory() as session:
            draft = await session.scalar(
                select(AutomationDraft).where(AutomationDraft.confirm_token == confirm_token)
            )
            if draft is None or draft.member_id != member.id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")
            if draft.status != "pending":
                raise HTTPException(status.HTTP_409_CONFLICT, "draft was already handled")
            if draft.expires_at < now:
                draft.status = "expired"
                await session.commit()
                raise HTTPException(status.HTTP_410_GONE, "draft has expired")

            raw = (yaml_content or draft.yaml_content).strip()
            try:
                config = parse_automation(raw, self._config.whitelist())
            except AutomationValidationError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid automation: {exc}"
                ) from exc

            rest = getattr(self._adapter, "rest", None)
            if rest is None:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "Home Assistant REST is not available"
                )
            automation_id = config.get("id") or f"ha_agent_{uuid.uuid4().hex[:12]}"
            try:
                await rest.write_automation(automation_id, config)
            except Exception as exc:  # noqa: BLE001 - 写入失败不改草稿状态
                logger.warning("chat.automation_write_failed", draft_id=draft.id)
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Home Assistant rejected the automation: {str(exc)[:300]}",
                ) from exc

            draft.status = "confirmed"
            draft.yaml_content = raw
            add_audit(
                session,
                member_id=member.id,
                action="automation.confirmed",
                target=automation_id,
                detail={
                    "draft_id": draft.id,
                    "alias": config.get("alias"),
                    "automation_id": automation_id,
                },
                ip=ip,
            )
            await session.commit()

        return {
            "confirmed": True,
            "draft_id": draft.id,
            "automation_id": automation_id,
            "alias": config.get("alias"),
        }

    # ---------- 会话查询 ----------

    async def list_sessions(self, member_id: int) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ChatSession)
                    .where(ChatSession.member_id == member_id)
                    .order_by(ChatSession.updated_at.desc())
                    .limit(50)
                )
            ).all()
        return [
            {
                "id": row.id,
                "title": row.title,
                "channel": row.channel or "",
                "channel_ref": row.channel_ref or "",
                "channel_label": await self._resolve_channel_label(
                    row.channel or "", row.channel_ref or ""
                ),
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ]

    async def _resolve_channel_label(self, channel: str, channel_ref: str) -> str:
        """按来源渠道解析成可读标识（如飞书按 chat_id 取群名）；失败回退原始 ref。"""
        resolver = self._channel_label_resolver
        if (
            resolver is not None
            and channel
            and channel_ref
        ):
            try:
                label = await resolver(channel, channel_ref)
                if label:
                    return str(label)
            except Exception:  # noqa: BLE001 - 解析失败不影响列表
                logger.exception(
                    "chat.channel_label_failed", channel=channel, channel_ref=channel_ref
                )
        return channel_ref

    async def create_session(self, member_id: int, title: str = "新会话") -> dict[str, Any]:
        async with self._session_factory() as session:
            row = ChatSession(
                member_id=member_id,
                title=(title or "新会话")[:60],
                updated_at=utcnow(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return {
            "id": row.id,
            "title": row.title,
            "channel": row.channel or "",
            "channel_ref": row.channel_ref or "",
            "channel_label": await self._resolve_channel_label(
                row.channel or "", row.channel_ref or ""
            ),
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    async def list_messages(
        self, member_id: int, session_id: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.member_id != member_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
            rows = (
                await session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.id.desc())
                    .limit(limit)
                )
            ).all()
        items = []
        for row in reversed(rows):
            route = None
            tool_calls: list[dict[str, Any]] = []
            if row.tool_calls_json:
                try:
                    parsed = json.loads(row.tool_calls_json)
                except json.JSONDecodeError:
                    parsed = []
                if isinstance(parsed, dict):
                    tool_calls = parsed.get("tool_calls") or []
                    route = parsed.get("route")
                elif isinstance(parsed, list):
                    tool_calls = parsed
            items.append(
                {
                    "id": row.id,
                    "role": row.role,
                    "content": row.content,
                    "tool_calls": tool_calls,
                    "route": route,
                    "created_at": _iso(row.created_at),
                }
            )
        return items

    async def delete_session(self, member_id: int, session_id: int) -> None:
        async with self._session_factory() as session:
            chat_session = await session.get(ChatSession, session_id)
            if chat_session is None or chat_session.member_id != member_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
            await session.execute(
                delete(ChatMessage).where(ChatMessage.session_id == session_id)
            )
            await session.delete(chat_session)
            await session.commit()
