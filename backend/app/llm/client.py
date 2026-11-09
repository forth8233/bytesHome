from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from app.llm.json_parse import extract_json_object
from app.llm.providers import get_provider
from app.services.config_service import ConfigService

logger = structlog.get_logger(__name__)

_JSON_HINT = "json"


@dataclass(slots=True)
class LLMResult:
    ok: bool = False
    parsed_ok: bool = False
    provider: str = ""
    model: str = ""
    content: str = ""
    data: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None
    reasoning_content: str | None = None
    tier: int = 3
    attempts: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)


def _has_json_hint(messages: list[dict[str, Any]]) -> bool:
    return any(_JSON_HINT in str(message.get("content", "")).lower() for message in messages)


def _ensure_json_hint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if _has_json_hint(messages):
        return messages
    return [*messages, {"role": "user", "content": "请以 json 格式输出结果。"}]


class LLMClient:
    """OpenAI 兼容客户端：三级 JSON 降级、单次重试、fallback provider、用量累计。"""

    def __init__(self, config: ConfigService, max_concurrency: int = 2) -> None:
        self._config = config
        self._usage_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        settings: dict[str, Any] | None = None,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "result",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        settings = settings or self._config.llm_settings("primary")
        provider = str(settings.get("provider") or "")
        model = str(settings.get("model_name") or "")
        base_url = str(settings.get("api_base") or "")
        api_key = str(settings.get("api_key") or "")

        result = LLMResult(provider=provider, model=model, messages=messages)
        if not provider or not model:
            result.error = "llm is not configured"
            return result
        try:
            meta = get_provider(provider)
        except ValueError as exc:
            result.error = str(exc)
            return result
        base_url = base_url or meta["default_base_url"]

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "n": 1,
            "max_tokens": int(max_tokens or settings.get("max_tokens") or 1024),
        }
        temp = temperature if temperature is not None else settings.get("temperature")
        if temp is not None and not meta.get("ignores_temperature"):
            payload["temperature"] = float(temp)

        if json_schema is not None and meta.get("supports_json_schema"):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": False},
            }
            result.tier = 1
        elif meta.get("supports_json_object"):
            payload["response_format"] = {"type": "json_object"}
            if meta.get("json_object_needs_prompt_hint"):
                payload["messages"] = _ensure_json_hint(payload["messages"])
            result.tier = 2
        elif json_schema is not None:
            result.tier = 3
            payload["messages"] = _ensure_json_hint(payload["messages"])

        timeout = float(settings.get("timeout") or 60)
        started = time.perf_counter()
        try:
            async with self._semaphore:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0)
                ) as client:
                    response = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key or 'ollama'}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
        except httpx.HTTPError as exc:
            result.error = f"cannot reach provider: {exc}"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            await self._record_usage(result)
            return result

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}: {response.text[:300]}"
            await self._record_usage(result)
            return result
        try:
            body = response.json()
        except ValueError:
            result.error = "provider returned non-json body"
            await self._record_usage(result)
            return result
        if not isinstance(body, dict):
            # 兼容接口偶发返回数组/字符串等非对象体，安全降级而非抛异常
            result.error = "provider returned malformed body"
            await self._record_usage(result)
            return result
        result.ok = True
        usage = body.get("usage") or {}
        result.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        result.completion_tokens = int(usage.get("completion_tokens") or 0)
        choices = body.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        result.content = str(message.get("content") or "")
        result.reasoning_content = message.get("reasoning_content")
        await self._record_usage(result)
        return result

    async def structured(
        self,
        messages: list[dict[str, Any]],
        model_cls: type[BaseModel],
        *,
        settings: dict[str, Any] | None = None,
        schema_name: str = "result",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        result = await self.chat(
            messages,
            settings=settings,
            json_schema=model_cls.model_json_schema(),
            schema_name=schema_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not result.ok:
            return result
        parsed = extract_json_object(result.content)
        if parsed is None:
            result.error = "no json object found in response"
            return result
        try:
            validated = model_cls.model_validate(parsed)
        except ValidationError as exc:
            first = exc.errors()[:3]
            result.error = f"schema validation failed: {first}"
            result.data = parsed
            return result
        result.data = validated.model_dump(mode="json")
        result.parsed_ok = True
        return result

    async def structured_with_fallback(
        self,
        messages: list[dict[str, Any]],
        model_cls: type[BaseModel],
        *,
        schema_name: str = "result",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        primary = self._config.llm_settings("primary")
        result = await self.structured(
            messages,
            model_cls,
            settings=primary,
            schema_name=schema_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        result.attempts = 1
        if result.parsed_ok:
            return result

        if result.content:
            retry_messages = [
                *result.messages,
                {"role": "assistant", "content": result.content},
                {
                    "role": "user",
                    "content": (
                        "上一次输出无法通过校验：" + str(result.error or "unknown")[:500]
                        + "。请只输出一个符合 json schema 的 JSON 对象，不要输出任何其他内容。"
                    ),
                },
            ]
            retry = await self.structured(
                retry_messages,
                model_cls,
                settings=primary,
                schema_name=schema_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            retry.attempts = 2
            if retry.parsed_ok:
                return retry
            result = retry

        fallback = self._config.llm_settings("fallback")
        if fallback.get("provider") and fallback.get("model_name"):
            fb = await self.structured(
                messages,
                model_cls,
                settings=fallback,
                schema_name=schema_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            fb.attempts = result.attempts + 1
            if not fb.parsed_ok and result.error:
                fb.error = f"primary: {result.error} | fallback: {fb.error}"
            return fb
        return result

    async def _record_usage(self, result: LLMResult) -> None:
        key = f"usage.llm.{datetime.now().strftime('%Y-%m-%d')}"
        async with self._usage_lock:
            current = dict(self._config.get(key) or {})
            current["calls"] = int(current.get("calls") or 0) + 1
            current["prompt_tokens"] = int(current.get("prompt_tokens") or 0) + result.prompt_tokens
            current["completion_tokens"] = int(current.get("completion_tokens") or 0) + (
                result.completion_tokens
            )
            if result.error:
                current["errors"] = int(current.get("errors") or 0) + 1
            try:
                await self._config.set(key, current)
            except Exception:  # noqa: BLE001 - 计数失败不影响主链路
                logger.exception("llm.usage.record_failed")
