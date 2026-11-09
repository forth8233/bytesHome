"""探测在线 LLM 提供商的连通性与可用模型。

密钥只从环境变量读取，不打印、不落盘、不写入仓库。

用法（仓库根目录）：
    MINIMAX_API_KEY=*** backend/.venv/Scripts/python.exe tools/test_llm_provider.py minimax
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def probe(base_url: str, api_key: str, model: str, timeout: float = 20.0) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "回复 ok"}],
        "max_tokens": 8,
    }
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return 0, f"network: {exc.__class__.__name__}"
    if response.status_code >= 400:
        return response.status_code, response.text[:180].replace("\n", " ")
    try:
        body = response.json()
        content = (
            body.get("choices", [{}])[0].get("message", {}).get("content", "")
            or body.get("reply")
            or ""
        )
        usage = body.get("usage") or {}
        return response.status_code, f"ok reply={content.strip()[:40]!r} usage={usage}"
    except ValueError:
        return response.status_code, "ok (non-json body)"


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "minimax"
    key_name = f"{provider.upper()}_API_KEY"
    api_key = os.environ.get(key_name, "")
    if not api_key:
        print(f"{key_name} is not set")
        return 2

    if provider == "minimax":
        bases = ["https://api.minimax.io/v1", "https://api.minimax.cn/v1"]
        models = [
            "MiniMax-M2",
            "MiniMax-M1",
            "MiniMax-Text-01",
            "abab6.5s-chat",
        ]
    elif provider == "deepseek":
        bases = ["https://api.deepseek.com/v1"]
        models = ["deepseek-chat"]
    else:
        print(f"unsupported provider: {provider}")
        return 2

    best: list[tuple[str, str]] = []
    for base in bases:
        for model in models:
            status, detail = probe(base, api_key, model)
            print(f"[{base}] model={model} -> HTTP {status} {detail}")
            if status == 200:
                best.append((base, model))

    print("---")
    if best:
        for base, model in best:
            print(f"available: base={base} model={model}")
        return 0
    print("no working model/base combination found")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
