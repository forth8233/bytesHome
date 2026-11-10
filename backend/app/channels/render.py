from __future__ import annotations

from typing import Any

from app.channels.base import OutboundMessage

_RISK_COLORS = {
    "high": "red",
    "medium": "orange",
    "low": "green",
}


def _state_lines(tool_results: list[dict[str, Any]]) -> list[str]:
    """把 get_state 工具结果压成「名称：状态」的短行，卡片和纯文本共用。"""
    lines: list[str] = []
    for item in tool_results:
        if not isinstance(item, dict):
            continue
        if item.get("tool") != "get_state":
            continue
        result = item.get("result") or {}
        persist = result.get("persist") if isinstance(result, dict) else None
        if not isinstance(persist, dict) or not persist.get("ok"):
            continue
        for entity in persist.get("entities") or []:
            name = str(entity.get("name") or entity.get("entity_id") or "")
            state = str(entity.get("state") or "")
            if name:
                lines.append(f"{name}：{state}")
    return lines


def _action_lines(proposal: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for action in proposal.get("actions") or []:
        if not isinstance(action, dict):
            continue
        target = action.get("entity_id") or ""
        domain = action.get("domain") or ""
        service = action.get("service") or ""
        data = action.get("data") or {}
        detail = "，".join(f"{key}={value}" for key, value in data.items())
        lines.append(
            f"{target} → {domain}.{service}" + (f"（{detail}）" if detail else "")
        )
    return lines


def _proposal_buttons(proposal_id: Any) -> dict[str, Any] | None:
    """确认 / 拒绝按钮；点击触发卡片回调，由 FeishuAdapter 翻译成文字指令。"""
    if not isinstance(proposal_id, int):
        return None
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "确认"},
                "type": "primary",
                "value": {"kind": "proposal", "id": proposal_id, "op": "confirm"},
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "拒绝"},
                "type": "danger",
                "value": {"kind": "proposal", "id": proposal_id, "op": "reject"},
            },
        ],
    }


# 点击按钮后回调应答里返回的新卡片：状态行文案 + 标题模板色
_PROCESSED_STATUS = {
    "confirm": ("green", "✅ 已确认：指令已提交执行。"),
    "reject": ("grey", "❌ 已拒绝：该提案不会执行。"),
}


def render_proposal_processed(card: dict[str, Any], op: str) -> dict[str, Any]:
    """按钮点击后的「已处理」卡片：去掉按钮、追加状态行、标题改色。

    由 FeishuAdapter 放进回调应答（card.type=raw），飞书会把原消息原地替换，
    无需再调更新 API。不修改传入的卡片对象。
    """
    processed = _PROCESSED_STATUS.get(op)
    if processed is None:
        return card
    template, status_text = processed
    elements = [
        element
        for element in card.get("elements") or []
        if isinstance(element, dict) and element.get("tag") != "action"
    ]
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": status_text}})
    header = dict(card.get("header") or {})
    header["template"] = template
    title = dict(header.get("title") or {})
    mark = "已确认" if op == "confirm" else "已拒绝"
    content = str(title.get("content") or "")
    if mark not in content:
        title["content"] = f"{content}（{mark}）"
    header["title"] = title
    return {**card, "header": header, "elements": elements}


def render_reply(result: dict[str, Any]) -> OutboundMessage:
    """对话结果 → 纯文本 + 飞书卡片。

    卡片只在有结构化内容（设备状态、待确认提案、自动化草稿）时才生成；
    其余情况用纯文本，避免每条回复都套一个空壳卡片。
    """
    reply = str(result.get("reply") or "").strip()
    state_lines = _state_lines(result.get("tool_results") or [])
    proposal = result.get("proposal")
    draft = result.get("draft")

    text_parts = [reply] if reply else []
    if state_lines:
        text_parts.append("当前状态：\n" + "\n".join(state_lines))
    if isinstance(proposal, dict) and proposal.get("id"):
        text_parts.append(
            f"待确认提案 #{proposal['id']}（{proposal.get('risk_level', 'low')}）："
            f"{proposal.get('suggestion', '')}\n"
            f"回复「确认 #{proposal['id']}」执行，回复「拒绝 #{proposal['id']}」放弃。"
        )
    if isinstance(draft, dict) and draft.get("confirm_token"):
        text_parts.append(
            f"自动化草稿待确认（令牌 {draft['confirm_token']}）：\n"
            f"{draft.get('yaml', '')[:1500]}\n"
            f"回复「确认自动化 {draft['confirm_token']}」写入 Home Assistant。"
        )
    plain = "\n\n".join(part for part in text_parts if part) or "（无回复）"

    card = None
    if state_lines or isinstance(proposal, dict) or isinstance(draft, dict):
        card = _build_card(
            title=_card_title(result, state_lines, proposal, draft),
            reply=reply,
            state_lines=state_lines,
            proposal=proposal if isinstance(proposal, dict) else None,
            draft=draft if isinstance(draft, dict) else None,
        )
    return OutboundMessage(text=plain, card=card)


def render_proposal_push(proposal: dict[str, Any]) -> OutboundMessage:
    """主动推送待确认提案（reasoner 侧产生，不经过用户对话）。"""
    proposal_id = proposal.get("id")
    risk = str(proposal.get("risk_level") or "low")
    suggestion = str(proposal.get("suggestion") or "").strip()
    entity = str(proposal.get("entity_id") or "")
    actions = _action_lines(proposal)

    text_parts = [f"[{risk}] 检测到需要确认的操作"]
    if entity:
        text_parts.append(f"设备：{entity}")
    if suggestion:
        text_parts.append(f"建议：{suggestion}")
    if actions:
        text_parts.append("动作：\n" + "\n".join(f"- {line}" for line in actions))
    text_parts.append(f"回复「确认 #{proposal_id}」执行，回复「拒绝 #{proposal_id}」放弃。")
    plain = "\n".join(text_parts)

    detail = [f"**风险等级**：{risk}"]
    if entity:
        detail.append(f"**设备**：{entity}")
    if suggestion:
        detail.append(f"**建议**：{suggestion}")
    if actions:
        detail.append("**动作**：\n" + "\n".join(f"- {line}" for line in actions))
    detail.append(f"回复「确认 #{proposal_id}」执行，回复「拒绝 #{proposal_id}」放弃。")
    card = _build_card(
        title=f"待确认操作 #{proposal_id}",
        reply="",
        state_lines=[],
        proposal=proposal,
        draft=None,
    )
    card["elements"] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(detail)[:3000]}}
    ]
    buttons = _proposal_buttons(proposal_id)
    if buttons:
        card["elements"].append(buttons)
    return OutboundMessage(text=plain, card=card)


def render_alert_push(
    *, entity_id: str, suggestion: str, risk_level: str = "high"
) -> OutboundMessage:
    """高风险告警推送（没有可执行动作时）。"""
    text = f"[{risk_level}][告警] {entity_id}：{str(suggestion or '')[:200]}"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _RISK_COLORS.get(risk_level, "red"),
            "title": {"tag": "plain_text", "content": "高风险告警"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**设备**：{entity_id}\n**说明**：{str(suggestion or '')[:500]}",
                },
            }
        ],
    }
    return OutboundMessage(text=text, card=card)


def _card_title(
    result: dict[str, Any],
    state_lines: list[str],
    proposal: Any,
    draft: Any,
) -> str:
    if isinstance(proposal, dict) and proposal.get("id"):
        return f"待确认操作 #{proposal['id']}"
    if isinstance(draft, dict) and draft.get("confirm_token"):
        return "自动化待确认"
    if state_lines:
        return "设备状态"
    route = result.get("route")
    return "快捷指令" if route == "fast_path" else "家庭助手"


def _build_card(
    *,
    title: str,
    reply: str,
    state_lines: list[str],
    proposal: dict[str, Any] | None,
    draft: dict[str, Any] | None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    if reply:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": reply[:3000]}}
        )
    if state_lines:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(f"- {line}" for line in state_lines[:30]),
                },
            }
        )
    if proposal:
        if elements:
            elements.append({"tag": "hr"})
        detail = [f"**风险等级**：{proposal.get('risk_level', 'low')}"]
        if proposal.get("entity_id"):
            detail.append(f"**设备**：{proposal['entity_id']}")
        if proposal.get("suggestion"):
            detail.append(f"**建议**：{proposal['suggestion']}")
        actions = _action_lines(proposal)
        if actions:
            detail.append("**动作**：\n" + "\n".join(f"- {line}" for line in actions))
        detail.append(
            f"回复「确认 #{proposal.get('id')}」执行，回复「拒绝 #{proposal.get('id')}」放弃。"
        )
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(detail)[:3000]},
            }
        )
        buttons = _proposal_buttons(proposal.get("id"))
        if buttons:
            elements.append(buttons)
    if draft:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"回复「确认自动化 {draft.get('confirm_token')}」写入 Home Assistant。\n"
                        f"```yaml\n{str(draft.get('yaml', ''))[:1200]}\n```"
                    ),
                },
            }
        )

    template = "blue"
    if proposal:
        template = _RISK_COLORS.get(str(proposal.get("risk_level")), "orange")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def render_schedule_receipt(
    *, ok: bool, summary: str, result_text: str = "", detail: dict[str, Any] | None = None
) -> OutboundMessage:
    """定时任务执行后的主动回执：告诉设定者任务已执行成功/失败。

    由 ScheduleRunner 在任务出账后调用，经 ChannelNotifier 推到设定者的渠道。
    ok=True 时字体用绿色，失败用红色；同 _build_card 结构，纯文本 + 可选卡片。
    """
    head = f"定时任务已完成：{summary}" if ok else f"定时任务执行失败：{summary}"
    lines = [head]
    if result_text:
        lines.append(result_text)
    if detail:
        for key, value in detail.items():
            if key == "actions":
                continue
            if isinstance(value, list):
                value = "；".join(str(v) for v in value)
            if value in ("", None):
                continue
            lines.append(f"{key}：{value}")
    plain = "\n".join(lines)

    card = _build_card(
        title=head,
        reply=plain,
        state_lines=[],
        proposal=None,
        draft=None,
    )
    card["header"]["template"] = "green" if ok else "red"
    return OutboundMessage(text=plain, card=card)
