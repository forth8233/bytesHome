from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import MemberEntityPermission

RISK_RANK = {"low": 0, "medium": 1, "high": 2}

RISK_BY_DOMAIN = {
    "lock": "high",
    "valve": "high",
    "alarm_control_panel": "high",
    "shell_command": "high",
    "hassio": "high",
    "homeassistant": "high",
    "cover": "medium",
    "climate": "medium",
    "water_heater": "medium",
    "switch": "medium",
    "fan": "medium",
    "humidifier": "medium",
    "script": "medium",
    "light": "low",
    "media_player": "low",
    "notify": "low",
    "scene": "low",
}

SERVICE_RISK_OVERRIDES = {
    "lock.unlock": "high",
    "lock.open": "high",
    "alarm_control_panel.alarm_disarm": "high",
    "homeassistant.restart": "high",
    "homeassistant.stop": "high",
}

HARD_DENY_DOMAINS = {"shell_command", "hassio", "homeassistant", "python_script"}

# 只取安全相关参数参与签名，自由文本与时间戳不参与
SIGNATURE_PARAM_KEYS = (
    "temperature",
    "target_temp_low",
    "target_temp_high",
    "brightness_pct",
    "brightness",
    "color_temp",
    "color_temp_kelvin",
    "rgb_color",
    "hvac_mode",
    "fan_mode",
    "preset_mode",
    "percentage",
    "position",
    "tilt_position",
    "volume_level",
    "source",
    "humidity",
    "operation_mode",
)

ENTITY_OPTIONAL_DOMAINS = {"notify"}


def max_risk(*levels: str) -> str:
    return max(levels, key=lambda level: RISK_RANK.get(level, 1))


def action_signature(service: str, entity_id: str | None, data: dict[str, Any]) -> str:
    relevant = {key: data.get(key) for key in SIGNATURE_PARAM_KEYS if key in data}
    blob = f"{service}|{entity_id or ''}|{json.dumps(relevant, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class GuardResult:
    passed: bool
    requires_confirm: bool
    risk_level: str
    reasons: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)


class ActionGuard:
    """所有设备动作的唯一入口：白名单、参数、成员权限与风险判定。"""

    def __init__(
        self,
        config,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory

    async def check(
        self,
        actions: list[Any],
        *,
        member=None,
        source: str = "proposal",
        event_pre_risk: str = "low",
        llm_risk: str = "low",
    ) -> GuardResult:
        rules = self._config.whitelist()
        reasons: list[str] = []
        normalized: list[dict[str, Any]] = []
        signatures: list[str] = []
        code_risk = "low"
        control_patterns = await self._control_patterns(member)

        if not actions:
            return GuardResult(False, False, "low", ["no actions to check"])

        for item in actions:
            action = item if isinstance(item, dict) else item.model_dump()
            domain = str(action.get("domain") or "").strip()
            service = str(action.get("service") or "").strip()
            entity_id = action.get("entity_id")
            data = dict(action.get("data") or {})
            full_service = f"{domain}.{service}"

            if not domain or not service:
                reasons.append("action is missing domain or service")
                continue
            if domain in HARD_DENY_DOMAINS or full_service.split(".", 1)[0] in HARD_DENY_DOMAINS:
                reasons.append(f"{full_service} is hard-denied")
                continue
            if not rules.service_allowed(full_service):
                reasons.append(f"service {full_service} is not whitelisted")
                continue
            if domain not in ENTITY_OPTIONAL_DOMAINS:
                if not entity_id:
                    reasons.append(f"service {full_service} requires entity_id")
                    continue
                if not rules.entity_allowed(str(entity_id)):
                    reasons.append(f"entity {entity_id} is not whitelisted")
                    continue
                if member is not None and member.role != "admin":
                    if not self._member_can_control(control_patterns, str(entity_id)):
                        reasons.append(f"member {member.username} cannot control {entity_id}")
                        continue

            param_error = rules.validate_params(full_service, data)
            if param_error:
                reasons.append(param_error)
                continue

            action_risk = max_risk(
                RISK_BY_DOMAIN.get(domain, "medium"),
                SERVICE_RISK_OVERRIDES.get(full_service, "low"),
            )
            code_risk = max_risk(code_risk, action_risk)
            signatures.append(action_signature(full_service, entity_id, data))
            normalized.append(
                {
                    "domain": domain,
                    "service": service,
                    "entity_id": entity_id,
                    "data": data,
                }
            )

        if reasons:
            return GuardResult(False, True, code_risk, reasons, normalized, signatures)

        final_risk = max_risk(code_risk, str(llm_risk or "low"), str(event_pre_risk or "low"))
        return GuardResult(
            passed=True,
            requires_confirm=final_risk == "high",
            risk_level=final_risk,
            reasons=[],
            actions=normalized,
            signatures=signatures,
        )

    @staticmethod
    def _member_can_control(patterns: list[MemberEntityPermission], entity_id: str) -> bool:
        return any(
            permission.can_control and fnmatch.fnmatchcase(entity_id, permission.entity_pattern)
            for permission in patterns
        )

    async def _control_patterns(self, member) -> list[MemberEntityPermission]:
        if member is None or member.role == "admin" or self._session_factory is None:
            return []
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(MemberEntityPermission).where(
                            MemberEntityPermission.member_id == member.id
                        )
                    )
                )
                .scalars()
                .all()
            )
