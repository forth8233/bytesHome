"""只读验证语音词库管理接口。

用法（在 backend 目录或仓库根目录均可）：
    backend/.venv/Scripts/python.exe tools/verify_voice_api.py

脚本直接在本地铸造管理员访问令牌用于请求，不会打印令牌；
只调用 GET 与解析预览接口，不执行任何设备动作。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT / "backend")
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.security import create_access_token  # noqa: E402
from app.db.models import Member  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402

BASE = os.environ.get("VOICE_API_BASE", "http://127.0.0.1:8000").rstrip("/")

PHRASES = [
    "客厅灯亮度调到50%",
    "空调温度调高2度",
    "灯光亮度调到50%",
    "关闭所有灯",
    "查询客厅灯光",
    "客厅调亮一点",
]


async def mint_token() -> str:
    async with SessionFactory() as session:
        admin = await session.scalar(
            select(Member).where(Member.role == "admin", Member.enabled.is_(True))
        )
    if admin is None:
        raise SystemExit("no enabled admin member found")
    token, _, _ = create_access_token(admin.id)
    return token


def request(method: str, path: str, token: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body


def summarize_voice(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"raw": payload}
    actions = payload.get("actions") or []
    return {
        "route": payload.get("route"),
        "intent": payload.get("intent"),
        "op": payload.get("op"),
        "function": payload.get("function"),
        "device_type": payload.get("device_type"),
        "targets": [
            item.get("name") or item.get("entity_id")
            for item in (payload.get("targets") or [])
        ][:5],
        "actions": len(actions),
        "reply": payload.get("reply"),
        "reason": payload.get("reason"),
        "guard": payload.get("guard"),
    }


def main() -> int:
    token = asyncio.run(mint_token())
    failures = 0

    status, body = request("GET", "/api/fastpath/voice/lexicons", token)
    print(f"[lexicons] HTTP {status} total={body.get('total') if isinstance(body, dict) else body}")
    if status != 200:
        failures += 1
    elif sum(item["total"] for item in body.get("items", [])) != body["total"]:
        print("  ! lexicon total mismatch")
        failures += 1

    status, body = request("GET", "/api/fastpath/voice/entries?page=1&page_size=3", token)
    print(f"[entries] HTTP {status} total={body.get('total') if isinstance(body, dict) else body}")
    if status != 200:
        failures += 1
    else:
        for item in body.get("items", []):
            print(f"  {item['lexicon']}: {item['alias']} -> {item['canonical']}")

    status, body = request(
        "GET", "/api/fastpath/voice/entries?lexicon=increase&keyword=%E8%B0%83&page=1&page_size=3", token
    )
    print(f"[entries/increase] HTTP {status} total={body.get('total') if isinstance(body, dict) else body}")
    if status != 200:
        failures += 1

    status, body = request("GET", "/api/fastpath/voice/profiles", token)
    count = len(body.get("items", [])) if isinstance(body, dict) else 0
    print(f"[profiles] HTTP {status} count={count}")
    if status != 200:
        failures += 1

    status, body = request("GET", "/api/stats/fastpath", token)
    if status == 200 and isinstance(body, dict):
        print(
            "[stats] voice_loaded=%s voice_entries=%s counters=%s"
            % (
                body.get("voice_loaded"),
                body.get("voice_entries"),
                {key: value for key, value in (body.get("counters") or {}).items() if key.startswith("voice")},
            )
        )
    else:
        print(f"[stats] HTTP {status}")
        failures += 1

    for phrase in PHRASES:
        status, body = request(
            "POST", "/api/fastpath/voice/test", token, {"text": phrase}
        )
        if status != 200:
            print(f"[test] {phrase} -> HTTP {status} {body}")
            failures += 1
            continue
        print(f"[test] {phrase} -> {json.dumps(summarize_voice(body), ensure_ascii=False)}")

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
