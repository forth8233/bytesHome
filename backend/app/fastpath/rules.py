from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.fastpath.normalize import normalize

NEGATIVE_WORDS = ("不要", "别", "取消", "不用", "停止开启")
TEMPLATE_TOKENS = ("{{", "{%", "$(", "<%", "%>")


@dataclass(frozen=True)
class SceneRule:
    id: int
    name: str
    priority: int
    phrases: tuple[str, ...]
    actions: tuple[dict[str, Any], ...]
    reply_template: str
    version: int


def scene_from_row(row) -> SceneRule | None:
    if not row.enabled or row.kind != "scene":
        return None
    try:
        match = json.loads(row.match_json or "{}")
        actions = json.loads(row.action_json or "[]")
    except (TypeError, ValueError):
        return None
    phrases = match.get("phrases") if isinstance(match, dict) else None
    if not isinstance(phrases, list) or not isinstance(actions, list) or not actions:
        return None
    cleaned = tuple(
        normalize(str(item)) for item in phrases if str(item).strip()
    )
    if not cleaned:
        return None
    return SceneRule(
        id=row.id,
        name=row.name,
        priority=row.priority,
        phrases=cleaned,
        actions=tuple(action for action in actions if isinstance(action, dict)),
        reply_template=row.reply_template or "",
        version=row.version,
    )


def match_scene(rules: list[SceneRule], text: str, *, exact: bool = True) -> SceneRule | None:
    normalized = normalize(text)
    if not normalized or any(word in normalized for word in NEGATIVE_WORDS):
        return None
    ordered = sorted(rules, key=lambda rule: (-rule.priority, rule.id))
    for rule in ordered:
        name = normalize(rule.name)
        candidates = (name, *rule.phrases)
        for phrase in candidates:
            if not phrase:
                continue
            if normalized == phrase or (not exact and phrase in normalized):
                return rule
    return None


def _has_template(value: str) -> bool:
    return any(token in value for token in TEMPLATE_TOKENS)


def validate_rule(actions: list[dict[str, Any]], whitelist) -> str | None:
    if not isinstance(actions, list) or not (1 <= len(actions) <= 20):
        return "actions must contain 1-20 items"
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            return f"action {index} must be an object"
        domain = str(action.get("domain") or "").strip()
        service = str(action.get("service") or "").strip()
        if not domain or not service:
            return f"action {index} is missing domain or service"
        full_service = f"{domain}.{service}"
        if _has_template(full_service):
            return f"action {index} contains template syntax"
        if not whitelist.service_allowed(full_service):
            return f"service {full_service} is not whitelisted"
        data = action.get("data") or {}
        if not isinstance(data, dict):
            return f"action {index} data must be an object"
        for key, value in data.items():
            if isinstance(value, str) and _has_template(value):
                return f"action {index} data contains template syntax"
        param_error = whitelist.validate_params(full_service, data)
        if param_error:
            return f"action {index}: {param_error}"

        target = action.get("target")
        if target is None:
            continue
        if not isinstance(target, dict):
            return f"action {index} target must be an object"
        target_type = target.get("type")
        if target_type == "entity":
            entity_id = str(target.get("entity_id") or "")
            if not entity_id:
                return f"action {index} entity target is missing entity_id"
            if _has_template(entity_id):
                return f"action {index} entity target contains template syntax"
            if not whitelist.entity_allowed(entity_id):
                return f"entity {entity_id} is not whitelisted"
        elif target_type == "area":
            if not str(target.get("area") or "").strip():
                return f"action {index} area target is missing area"
        elif target_type == "domain_all":
            if not str(target.get("domain") or "").strip():
                return f"action {index} domain_all target is missing domain"
        else:
            return f"action {index} has unsupported target type"
    return None


def validate_phrases(phrases: list[str]) -> str | None:
    if not isinstance(phrases, list) or not (1 <= len(phrases) <= 20):
        return "phrases must contain 1-20 items"
    for phrase in phrases:
        if not isinstance(phrase, str) or not phrase.strip():
            return "phrase must be a non-empty string"
        if len(phrase) > 64:
            return "phrase is too long"
        if _has_template(phrase):
            return "phrase contains template syntax"
    return None
