from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, ConfigDep, HADep
from app.ha.adapter import HAAdapter
from app.llm.providers import PROVIDERS, get_provider, probe
from app.safety.whitelist import WhitelistRules
from app.services.config_service import DEFAULT_GLOBAL, DEFAULT_LLM

router = APIRouter(prefix="/api/config", tags=["config"])


class HAConfigIn(BaseModel):
    base_url: str = Field(min_length=8, max_length=255)
    token: str | None = None
    test: bool = True


class WhitelistIn(BaseModel):
    entities: dict[str, list[str]] = Field(default_factory=dict)
    services: dict[str, list[str]] = Field(default_factory=dict)
    param_rules: dict[str, dict[str, list[float]]] = Field(default_factory=dict)


class LLMConfigIn(BaseModel):
    provider: str
    model_name: str = ""
    api_base: str | None = None
    api_key: str | None = None
    temperature: float = 0.1
    timeout: int = 60
    max_tokens: int = 1024
    fallback_provider: str | None = None
    fallback_model: str | None = None
    fallback_api_base: str | None = None
    fallback_api_key: str | None = None


class LLMTestIn(BaseModel):
    which: str = "primary"
    provider: str | None = None
    model_name: str | None = None
    api_base: str | None = None
    api_key: str | None = None


def _mask_llm(config: dict[str, Any]) -> dict[str, Any]:
    masked = dict(config)
    masked["api_key"] = "***" if config.get("api_key") else ""
    return masked


@router.get("/ha")
async def get_ha_config(_: AdminUser, config: ConfigDep, ha: HADep) -> dict[str, Any]:
    base_url, token = config.ha_settings()
    return {
        "base_url": base_url,
        "token_set": bool(token),
        "connected": ha.connected,
        "last_error": ha.last_error,
        "last_snapshot_at": ha.last_snapshot_at,
        "cached_entities": len(ha.cache),
    }


@router.post("/ha")
async def update_ha_config(
    payload: HAConfigIn, _: AdminUser, config: ConfigDep, ha: HADep
) -> dict[str, Any]:
    base_url = payload.base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "base_url must be http(s)")
    await config.set("ha.base_url", base_url)
    if payload.token:
        await config.set("ha.token", payload.token, encrypted=True)

    result: dict[str, Any] | None = None
    if payload.test:
        _, token = config.ha_settings()
        ok, message = await HAAdapter.test_connection(base_url, token)
        result = {"ok": ok, "message": message}

    await ha.stop()
    await ha.start()
    return {"saved": True, "test": result}


@router.get("/whitelist")
async def get_whitelist(_: AdminUser, config: ConfigDep) -> Any:
    return config.get("whitelist")


@router.post("/whitelist")
async def update_whitelist(
    payload: WhitelistIn, _: AdminUser, config: ConfigDep, ha: HADep
) -> Any:
    data = payload.model_dump()
    WhitelistRules.from_dict(data)  # validate shape early
    await config.set("whitelist", data)
    allowed = await ha.resync() if ha.rest is not None else 0
    return {"saved": True, "visible_entities": allowed}


@router.get("/global")
async def get_global_config(_: AdminUser, config: ConfigDep) -> dict[str, Any]:
    return config.global_settings()


@router.post("/global")
async def update_global_config(
    payload: dict[str, Any], _: AdminUser, config: ConfigDep
) -> dict[str, Any]:
    unknown = set(payload) - set(DEFAULT_GLOBAL)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown settings: {sorted(unknown)}"
        )
    current = config.global_settings()
    current.update(payload)
    await config.set("global", current)
    return current


@router.get("/llm")
async def get_llm_config(_: AdminUser, config: ConfigDep) -> dict[str, Any]:
    fallback = config.get("llm.fallback")
    return {
        "primary": _mask_llm(config.llm_settings("primary")),
        "fallback": _mask_llm(fallback) if isinstance(fallback, dict) else None,
    }


@router.get("/llm/providers")
async def list_providers(_: AdminUser) -> dict[str, Any]:
    return {"providers": PROVIDERS, "defaults": DEFAULT_LLM}


@router.post("/llm")
async def update_llm_config(payload: LLMConfigIn, _: AdminUser, config: ConfigDep) -> dict[str, Any]:
    try:
        get_provider(payload.provider)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if payload.fallback_provider:
        try:
            get_provider(payload.fallback_provider)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    current = config.llm_settings("primary")
    api_key = payload.api_key
    if api_key in (None, "", "***"):
        api_key = current.get("api_key", "")

    primary = {
        "provider": payload.provider,
        "model_name": payload.model_name,
        "api_base": payload.api_base or current.get("api_base") or "http://localhost:11434/v1",
        "api_key": api_key,
        "temperature": payload.temperature,
        "timeout": payload.timeout,
        "max_tokens": payload.max_tokens,
    }
    await config.set("llm.primary", primary, encrypted=True)

    fallback = None
    if payload.fallback_provider:
        same_provider = payload.fallback_provider == payload.provider
        fallback_meta = get_provider(payload.fallback_provider)
        fallback_key = payload.fallback_api_key
        if fallback_key in (None, "", "***"):
            fallback_key = api_key if same_provider else ""
        fallback = {
            "provider": payload.fallback_provider,
            "model_name": payload.fallback_model or "",
            "api_base": payload.fallback_api_base
            or (primary["api_base"] if same_provider else fallback_meta["default_base_url"]),
            "api_key": fallback_key,
            "temperature": payload.temperature,
            "timeout": payload.timeout,
            "max_tokens": payload.max_tokens,
        }
    await config.set("llm.fallback", fallback, encrypted=True)

    return {"saved": True, "primary": _mask_llm(primary), "fallback": _mask_llm(fallback) if fallback else None}


@router.post("/llm/test")
async def test_llm(payload: LLMTestIn, _: AdminUser, config: ConfigDep) -> dict[str, Any]:
    if payload.which not in ("primary", "fallback"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "which must be primary or fallback"
        )
    current = config.llm_settings(payload.which)
    provider = payload.provider or current.get("provider")
    if not provider:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"llm.{payload.which} is not configured"
        )
    try:
        meta = get_provider(provider)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    api_key = payload.api_key
    if api_key in (None, "", "***"):
        api_key = current.get("api_key", "")
    ok, message = await probe(
        provider=provider,
        base_url=payload.api_base or current.get("api_base") or meta["default_base_url"],
        api_key=api_key or "",
        model=payload.model_name or current.get("model_name") or "",
    )
    return {"ok": ok, "message": message}
