from typing import Any

import httpx

PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "label": "Ollama（本地）",
        "default_base_url": "http://localhost:11434/v1",
        "requires_api_key": False,
        "supports_json_schema": False,
        "supports_json_object": True,
        "ignores_temperature": False,
        "privacy": "local",
    },
    "deepseek": {
        "label": "DeepSeek（在线）",
        "default_base_url": "https://api.deepseek.com/v1",
        "requires_api_key": True,
        "supports_json_schema": False,
        "supports_json_object": True,
        "json_object_needs_prompt_hint": True,
        "returns_reasoning_content": True,
        "ignores_temperature": False,
        "privacy": "online",
        "notes": "response_format=json_schema 实测返回 400 unavailable；json_object 需 prompt 含 json 字样",
    },
    "minimax": {
        "label": "MiniMax（在线）",
        "default_base_url": "https://api.minimax.io/v1",
        "requires_api_key": True,
        "supports_json_schema": False,
        "supports_json_object": True,
        "ignores_temperature": True,
        "privacy": "online",
        "base_url_options": {
            "international": "https://api.minimax.io/v1",
            "china": "https://api.minimax.cn/v1",
        },
    },
}


def get_provider(name: str) -> dict[str, Any]:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise ValueError(f"unknown provider: {name}") from exc


async def probe(
    provider: str, base_url: str, api_key: str, model: str, timeout: float = 20.0
) -> tuple[bool, str]:
    meta = get_provider(provider)
    if not model:
        return False, "model name is required"
    if meta["requires_api_key"] and not api_key:
        return False, "api key is required for this provider"
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key or 'ollama'}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return False, f"cannot reach provider: {exc}"
    if response.status_code >= 400:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    return True, "ok"
