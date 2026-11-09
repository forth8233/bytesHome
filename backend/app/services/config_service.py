import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.db.models import SystemConfig
from app.safety.whitelist import WhitelistRules

DEFAULT_GLOBAL: dict[str, Any] = {
    "active_reasoning_enabled": True,
    "debounce_seconds": {"sensor": 10, "binary_sensor": 1, "default": 3},
    "storm_limit": {
        "per_entity_per_minute": 6,
        "per_domain_per_minute": 30,
        "storm_interval_seconds": 300,
    },
    "proposal_timeout_seconds": 900,
    "proposal_dedupe_seconds": 600,
    "feedback_cooldown_seconds": 3600,
    "feedback_reject_threshold": 2,
    "notify_min_interval_seconds": 300,
    "llm_rate_limit_per_minute": 30,
    "retention": {"event_days": 30, "trace_days": 30, "audit_days": 180, "chat_days": 90},
    "fast_path": {
        "enabled": True,
        "state_max_age_seconds": 60,
        "session_entity_ttl_seconds": 120,
        "max_candidates": 3,
        "member_rate_limit_per_minute": 30,
        "voice_enabled": True,
        "voice_max_group_actions": 50,
        "voice_default_steps": {
            "brightness": 10,
            "colorTemp": 500,
            "course": 10,
            "angle": 10,
            "targetTemp": 1.0,
            "volume": 10,
            "speed": 1,
            "targetHumidity": 5,
        },
    },
}

DEFAULT_LLM: dict[str, Any] = {
    "provider": "ollama",
    "model_name": "",
    "api_base": "http://localhost:11434/v1",
    "api_key": "",
    "temperature": 0.1,
    "timeout": 60,
    "max_tokens": 1024,
}

DEFAULT_WHITELIST: dict[str, Any] = {
    "entities": {"allow": [], "deny": []},
    "services": {"allow": [], "deny": []},
    "param_rules": {
        "climate.set_temperature": {"temperature": [16, 30]},
        "light.turn_on": {"brightness_pct": [1, 100]},
    },
}


class ConfigService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._cache: dict[str, Any] = {}

    async def load(self) -> None:
        from sqlalchemy import select

        async with self._session_factory() as session:
            rows = (await session.execute(select(SystemConfig))).scalars().all()
        cache: dict[str, Any] = {}
        for row in rows:
            raw = decrypt(row.value) if row.encrypted else row.value
            cache[row.key] = json.loads(raw)
        self._cache = cache

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    async def set(self, key: str, value: Any, *, encrypted: bool = False) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        stored = encrypt(payload) if encrypted else payload
        async with self._session_factory() as session:
            row = await session.get(SystemConfig, key)
            if row is None:
                session.add(SystemConfig(key=key, value=stored, encrypted=encrypted))
            else:
                row.value = stored
                row.encrypted = encrypted
            await session.commit()
        self._cache[key] = value

    async def seed_defaults(self) -> None:
        seeded = False
        for key, value in (
            ("global", DEFAULT_GLOBAL),
            ("llm.primary", DEFAULT_LLM),
            ("llm.fallback", None),
            ("whitelist", DEFAULT_WHITELIST),
        ):
            if key not in self._cache:
                await self.set(key, value)
                seeded = True
        if seeded:
            await self.load()

    def whitelist(self) -> WhitelistRules:
        return WhitelistRules.from_dict(self.get("whitelist", DEFAULT_WHITELIST))

    def global_settings(self) -> dict[str, Any]:
        merged = dict(DEFAULT_GLOBAL)
        merged.update(self.get("global", {}) or {})
        fast_path = dict(DEFAULT_GLOBAL["fast_path"])
        fast_path.update(merged.get("fast_path") or {})
        merged["fast_path"] = fast_path
        return merged

    def llm_settings(self, which: str = "primary") -> dict[str, Any]:
        value = self.get(f"llm.{which}")
        if value is None:
            # 未配置的 fallback 必须返回空字典，否则会被误认成可用的默认 provider
            return {} if which == "fallback" else dict(DEFAULT_LLM)
        merged = dict(DEFAULT_LLM)
        merged.update(value)
        return merged

    def ha_settings(self) -> tuple[str, str]:
        settings = get_settings()
        base_url = self.get("ha.base_url") or settings.ha_base_url
        token = self.get("ha.token") or settings.ha_token
        return str(base_url).rstrip("/"), str(token)
