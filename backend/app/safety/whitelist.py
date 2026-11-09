import fnmatch
from dataclasses import dataclass, field
from typing import Any


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


@dataclass(frozen=True)
class WhitelistRules:
    entity_allow: tuple[str, ...] = ()
    entity_deny: tuple[str, ...] = ()
    service_allow: tuple[str, ...] = ()
    service_deny: tuple[str, ...] = ()
    param_rules: dict[str, dict[str, list[float]]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WhitelistRules":
        data = data or {}
        entities = data.get("entities") or {}
        services = data.get("services") or {}
        return cls(
            entity_allow=tuple(entities.get("allow") or ()),
            entity_deny=tuple(entities.get("deny") or ()),
            service_allow=tuple(services.get("allow") or ()),
            service_deny=tuple(services.get("deny") or ()),
            param_rules=data.get("param_rules") or {},
        )

    def entity_allowed(self, entity_id: str) -> bool:
        if _matches(entity_id, self.entity_deny):
            return False
        return _matches(entity_id, self.entity_allow)

    def service_allowed(self, service: str) -> bool:
        if _matches(service, self.service_deny):
            return False
        return _matches(service, self.service_allow)

    def validate_params(self, service: str, data: dict[str, Any]) -> str | None:
        rules = self.param_rules.get(service)
        if not rules:
            return None
        for key, bounds in rules.items():
            if key not in data or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                continue
            try:
                value = float(data[key])
            except (TypeError, ValueError):
                return f"{service}.{key} must be numeric"
            low, high = float(bounds[0]), float(bounds[1])
            if not low <= value <= high:
                return f"{service}.{key} must be between {low} and {high}"
        return None
