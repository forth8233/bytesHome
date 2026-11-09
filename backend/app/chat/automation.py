from __future__ import annotations

import re
from typing import Any

import yaml

from app.safety.guard import HARD_DENY_DOMAINS

TOP_KEYS = {
    "id",
    "alias",
    "description",
    "trigger",
    "triggers",
    "condition",
    "conditions",
    "action",
    "actions",
    "mode",
    "max",
    "max_exceeded",
    "variables",
    "initial_state",
    "trace",
    "unique_id",
}

TRIGGER_PLATFORMS = {
    "state",
    "numeric_state",
    "time",
    "time_pattern",
    "sun",
    "zone",
    "device",
    "homeassistant",
    "event",
    "tag",
    "calendar",
}

TARGET_FORBIDDEN_KEYS = {"device_id", "area_id", "floor_id", "label_id"}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_YAML_LENGTH = 8000
MAX_ADMIN_YAML_LENGTH = 40000
MAX_DEPTH = 12


class AutomationValidationError(ValueError):
    pass


def _fail(message: str) -> None:
    raise AutomationValidationError(message)


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(f"{label} 必须是列表")
    return value


def _entity_ids(node: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("entity_id",):
        value = node.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(item) for item in value if isinstance(item, str))
    target = node.get("target")
    if isinstance(target, dict):
        for key in TARGET_FORBIDDEN_KEYS:
            if key in target:
                _fail(f"不支持通过 target.{key} 间接指定设备，请使用 entity_id")
        value = target.get("entity_id")
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(str(item) for item in value if isinstance(item, str))
    data = node.get("data")
    if isinstance(data, dict) and isinstance(data.get("entity_id"), (str, list)):
        value = data["entity_id"]
        found.extend([value] if isinstance(value, str) else [str(item) for item in value])
    return found


def _check_service(node: dict[str, Any], rules) -> None:
    full = node.get("service") or node.get("action")
    if not isinstance(full, str) or "." not in full:
        _fail("service 必须是 domain.service 格式")
    domain = full.split(".", 1)[0]
    if domain in HARD_DENY_DOMAINS:
        _fail(f"服务 {full} 被安全策略禁用")
    if not rules.service_allowed(full):
        _fail(f"服务 {full} 不在白名单内")

    data = node.get("data") or {}
    if not isinstance(data, dict):
        _fail(f"{full} 的 data 必须是对象")
    param_error = rules.validate_params(full, data)
    if param_error:
        _fail(param_error)

    entities = _entity_ids(node)
    if domain != "notify":
        if not entities:
            _fail(f"{full} 必须指定 entity_id")
        for entity_id in entities:
            if not rules.entity_allowed(entity_id):
                _fail(f"实体 {entity_id} 不在白名单内")


def _validate_action(items: list[Any], rules, depth: int, seen: set[int]) -> None:
    if depth > MAX_DEPTH:
        _fail("自动化嵌套层级过深")
    for item in items:
        if not isinstance(item, dict):
            _fail("action 列表中的每一项必须是对象")
        marker = id(item)
        if marker in seen:
            _fail("自动化结构包含循环引用")
        seen.add(marker)
        try:
            if "device_id" in item:
                _fail("不支持通过 device_id 间接指定设备，请使用 entity_id")
            if "event" in item:
                _fail("MVP 不支持 event 动作")
            if "template" in item:
                _fail("MVP 不支持 template 动作")
            if "wait_template" in item:
                _fail("MVP 不支持 wait_template")
            service = item.get("service")
            action_value = item.get("action")
            if isinstance(service, str) or isinstance(action_value, str):
                _check_service(item, rules)
                continue
            if "choose" in item:
                for option in _as_list(item["choose"], "choose"):
                    if not isinstance(option, dict):
                        _fail("choose 的每一项必须是对象")
                    _validate_action(
                        _as_list(option.get("sequence"), "choose.sequence"),
                        rules,
                        depth + 1,
                        seen,
                    )
                if "default" in item:
                    _validate_action(
                        _as_list(item["default"], "choose.default"), rules, depth + 1, seen
                    )
                continue
            if "if" in item:
                for branch in ("then", "else"):
                    if branch in item:
                        _validate_action(
                            _as_list(item[branch], branch), rules, depth + 1, seen
                        )
                continue
            if "repeat" in item:
                repeat = item["repeat"]
                if not isinstance(repeat, dict):
                    _fail("repeat 必须是对象")
                _validate_action(
                    _as_list(repeat.get("sequence"), "repeat.sequence"), rules, depth + 1, seen
                )
                continue
            if "parallel" in item:
                for branch in _as_list(item["parallel"], "parallel"):
                    if not isinstance(branch, dict):
                        _fail("parallel 的每一项必须是对象")
                    _validate_action(
                        _as_list(branch.get("sequence"), "parallel.sequence"),
                        rules,
                        depth + 1,
                        seen,
                    )
                continue
            if "sequence" in item:
                _validate_action(
                    _as_list(item["sequence"], "sequence"), rules, depth + 1, seen
                )
                continue
            allowed_leaf = {"delay", "wait_for_trigger", "condition", "stop", "variables", "alias", "enabled", "continue_on_error"}
            if set(item) & allowed_leaf:
                if "data_template" in item:
                    _fail("MVP 不支持 data_template")
                continue
            _fail(f"不支持的 action 类型：{sorted(item)[:5]}")
        finally:
            seen.discard(marker)


def _validate_triggers(triggers: list[Any]) -> None:
    if not triggers:
        _fail("必须至少包含一个 trigger")
    for trigger in triggers:
        if not isinstance(trigger, dict):
            _fail("trigger 列表中的每一项必须是对象")
        platform = trigger.get("platform") or trigger.get("trigger")
        if not isinstance(platform, str):
            _fail("trigger 缺少 platform")
        if platform not in TRIGGER_PLATFORMS:
            _fail(f"MVP 不支持 trigger platform: {platform}")


def parse_automation(raw: str, rules) -> dict[str, Any]:
    """解析并做结构安全校验，返回可写入 HA 的规范化配置。"""
    if not raw or len(raw) > MAX_YAML_LENGTH:
        _fail(f"YAML 不能为空且不超过 {MAX_YAML_LENGTH} 字符")
    if "{{" in raw or "{%" in raw:
        _fail("MVP 不支持 Jinja 模板")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        _fail(f"YAML 解析失败：{exc}")
    if not isinstance(data, dict):
        _fail("YAML 顶层必须是单个 automation 对象")
    if "automation" in data:
        _fail("请直接提供单条 automation 配置，不要外层再用 automation 包裹")

    unknown = set(data) - TOP_KEYS
    if unknown:
        _fail(f"不支持的字段：{sorted(unknown)[:5]}")
    if not isinstance(data.get("alias"), str) or not data["alias"].strip():
        _fail("alias 必填")
    if len(data["alias"]) > 128:
        _fail("alias 过长")

    if "trigger" in data and "triggers" in data:
        _fail("trigger 与 triggers 只能使用一种写法")
    if "action" in data and "actions" in data:
        _fail("action 与 actions 只能使用一种写法")
    triggers = _as_list(data.get("trigger", data.get("triggers")), "trigger")
    actions = _as_list(data.get("action", data.get("actions")), "action")
    _validate_triggers(triggers)
    _validate_action(actions, rules, 0, set())

    config = dict(data)
    if "triggers" in config:
        config["trigger"] = config.pop("triggers")
    if "actions" in config:
        config["action"] = config.pop("actions")
    if "conditions" in config:
        config["condition"] = config.pop("conditions")
    automation_id = config.get("id")
    if automation_id is not None and (
        not isinstance(automation_id, str) or not SAFE_ID.match(automation_id)
    ):
        _fail("id 只能包含字母、数字、下划线和短横线")
    return config


def _admin_service_check(node: dict[str, Any], rules) -> None:
    """管理台编辑用的服务校验：保留危险域拦截，不限制模板与触发器种类。"""
    full = node.get("service") or node.get("action")
    if not isinstance(full, str) or "." not in full:
        _fail("service 必须是 domain.service 格式")
    domain = full.split(".", 1)[0]
    if domain in HARD_DENY_DOMAINS:
        _fail(f"服务 {full} 被安全策略禁用")
    if rules.service_allowed(full):
        return
    denied = getattr(rules, "service_deny", ())
    if any(denied_item == full or denied_item.endswith(f".{full}") for denied_item in denied):
        _fail(f"服务 {full} 已被安全策略禁用")
    data = node.get("data") or {}
    if isinstance(data, dict):
        param_error = rules.validate_params(full, data)
        if param_error:
            _fail(param_error)


def _walk_admin_actions(items: list[Any], rules, depth: int) -> None:
    if depth > MAX_DEPTH:
        _fail("自动化嵌套层级过深")
    for item in items:
        if not isinstance(item, dict):
            _fail("action 列表中的每一项必须是对象")
        service = item.get("service")
        action_value = item.get("action")
        if isinstance(service, str) or isinstance(action_value, str):
            _admin_service_check(item, rules)
            continue
        for key in ("sequence",):
            if key in item:
                _walk_admin_actions(_as_list(item[key], key), rules, depth + 1)
        if "choose" in item:
            for option in _as_list(item["choose"], "choose"):
                if isinstance(option, dict):
                    _walk_admin_actions(
                        _as_list(option.get("sequence"), "choose.sequence"),
                        rules,
                        depth + 1,
                    )
            if "default" in item:
                _walk_admin_actions(
                    _as_list(item["default"], "choose.default"), rules, depth + 1
                )
        if "if" in item:
            for branch in ("then", "else"):
                if branch in item:
                    _walk_admin_actions(
                        _as_list(item[branch], branch), rules, depth + 1
                    )
        if "repeat" in item:
            repeat = item["repeat"]
            if isinstance(repeat, dict):
                _walk_admin_actions(
                    _as_list(repeat.get("sequence"), "repeat.sequence"),
                    rules,
                    depth + 1,
                )
        if "parallel" in item:
            for branch in _as_list(item["parallel"], "parallel"):
                if isinstance(branch, dict):
                    _walk_admin_actions(
                        _as_list(branch.get("sequence"), "parallel.sequence"),
                        rules,
                        depth + 1,
                    )


def parse_automation_admin(raw: str, rules) -> dict[str, Any]:
    """管理台编辑已有自动化：允许模板等 HA 完整能力，但仍拦截危险服务。"""
    if not raw or len(raw) > MAX_ADMIN_YAML_LENGTH:
        _fail(f"YAML 不能为空且不超过 {MAX_ADMIN_YAML_LENGTH} 字符")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        _fail(f"YAML 解析失败：{exc}")
    if not isinstance(data, dict):
        _fail("YAML 顶层必须是单个 automation 对象")
    if "automation" in data:
        _fail("请直接提供单条 automation 配置，不要外层再用 automation 包裹")
    unknown = set(data) - TOP_KEYS
    if unknown:
        _fail(f"不支持的字段：{sorted(unknown)[:5]}")
    if not isinstance(data.get("alias"), str) or not data["alias"].strip():
        _fail("alias 必填")
    if len(data["alias"]) > 128:
        _fail("alias 过长")
    if "trigger" in data and "triggers" in data:
        _fail("trigger 与 triggers 只能使用一种写法")
    if "action" in data and "actions" in data:
        _fail("action 与 actions 只能使用一种写法")

    config = dict(data)
    if "triggers" in config:
        config["trigger"] = config.pop("triggers")
    if "actions" in config:
        config["action"] = config.pop("actions")
    if "conditions" in config:
        config["condition"] = config.pop("conditions")
    triggers = _as_list(config.get("trigger", config.get("triggers")), "trigger")
    actions = _as_list(config.get("action", config.get("actions")), "action")
    if not triggers:
        _fail("必须至少包含一个 trigger")
    _walk_admin_actions(actions, rules, 0)

    automation_id = config.get("id")
    if automation_id is not None and (
        not isinstance(automation_id, str) or not SAFE_ID.match(automation_id)
    ):
        _fail("id 只能包含字母、数字、下划线和短横线")
    return config
