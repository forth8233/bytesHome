from __future__ import annotations

from typing import Any, Mapping

from app.events.models import RiskLevel

_HIGH_CLASSES = frozenset({"moisture", "smoke", "gas", "carbon_monoxide"})
_MEDIUM_CLASSES = frozenset({"door", "window", "opening"})


def pre_risk(domain: str, attributes: Mapping[str, Any] | None, new_state: str | None) -> RiskLevel:
    """代码内规则表，事件阶段只做粗打标，最终风险由 ActionGuard 就高计算。"""
    attributes = attributes or {}
    if domain == "binary_sensor" and new_state == "on":
        device_class = str(attributes.get("device_class") or "")
        if device_class in _HIGH_CLASSES:
            return "high"
        if device_class in _MEDIUM_CLASSES:
            return "medium"
    if domain in ("alarm_control_panel", "lock"):
        return "high"
    return "low"
