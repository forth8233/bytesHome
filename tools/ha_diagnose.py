"""Home Assistant 联调诊断脚本。

覆盖方案依赖的全部 HA 通路：REST 状态拉取、WebSocket 握手鉴权、
state_changed 事件订阅、实体/设备/区域注册表查询。

用法（在项目根目录执行）：
    backend/.venv/Scripts/python.exe tools/ha_diagnose.py
    backend/.venv/Scripts/python.exe tools/ha_diagnose.py --listen 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = ROOT / "backend" / ".env"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def mask(token: str) -> str:
    return f"{token[:6]}...{token[-4:]} (len={len(token)})" if token else "<empty>"


def ok(label: str, detail: str = "") -> None:
    print(f"  [OK]   {label}{(' — ' + detail) if detail else ''}")


def fail(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}{(' — ' + detail) if detail else ''}")


async def check_rest(base_url: str, token: str) -> tuple[bool, httpx.AsyncClient | None]:
    print("\n[REST]")
    headers = {"Authorization": f"Bearer {token}"}
    client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15.0)
    try:
        resp = await client.get("/api/")
        if resp.status_code != 200:
            fail("GET /api/", f"HTTP {resp.status_code}: {resp.text[:200]}")
            return False, client
        ok("GET /api/", resp.json().get("message", ""))

        resp = await client.get("/api/config")
        if resp.status_code != 200:
            fail("GET /api/config", f"HTTP {resp.status_code}")
            return False, client
        cfg = resp.json()
        ok(
            "GET /api/config",
            f"version={cfg.get('version')} tz={cfg.get('time_zone')} "
            f"name={cfg.get('location_name')}",
        )

        resp = await client.get("/api/states")
        if resp.status_code != 200:
            fail("GET /api/states", f"HTTP {resp.status_code}")
            return False, client
        states = resp.json()
        domains = Counter(item["entity_id"].split(".")[0] for item in states)
        ok("GET /api/states", f"{len(states)} 个实体，{len(domains)} 个域")
        top = ", ".join(f"{d}={n}" for d, n in domains.most_common(8))
        print(f"         主要域: {top}")
        return True, client
    except httpx.HTTPError as exc:
        fail("REST 连接", repr(exc))
        return False, client


async def ws_command(ws, msg_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    await ws.send(json.dumps({"id": msg_id, **payload}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        data = json.loads(raw)
        if data.get("id") == msg_id:
            return data


async def check_ws(base_url: str, token: str, listen_seconds: int) -> bool:
    print("\n[WebSocket]")
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    ws_url += "/api/websocket"
    try:
        async with websockets.connect(ws_url, max_size=None, open_timeout=15) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if first.get("type") != "auth_required":
                fail("握手", f"期望 auth_required，实际 {first.get('type')}")
                return False
            ok("握手", f"ha_version={first.get('ha_version')}")

            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if auth.get("type") != "auth_ok":
                fail("鉴权", str(auth.get("message")))
                return False
            ok("鉴权", "auth_ok")

            sub = await ws_command(ws, 1, {"type": "subscribe_events", "event_type": "state_changed"})
            if not sub.get("success"):
                fail("subscribe_events", str(sub.get("error")))
                return False
            ok("subscribe_events", "state_changed 已订阅")

            for msg_id, cmd, label in (
                (2, "config/entity_registry/list", "实体注册表"),
                (3, "config/device_registry/list", "设备注册表"),
                (4, "config/area_registry/list", "区域注册表"),
            ):
                res = await ws_command(ws, msg_id, {"type": cmd})
                if res.get("success"):
                    ok(label, f"{len(res.get('result') or [])} 条")
                else:
                    fail(label, str(res.get("error")))

            print(f"\n[事件] 监听 {listen_seconds}s（请在 HA 里操作设备，或等待自然变化）")
            seen: list[dict[str, Any]] = []
            deadline = asyncio.get_running_loop().time() + listen_seconds
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                data = json.loads(raw)
                if data.get("type") != "event":
                    continue
                event = data.get("event") or {}
                if event.get("event_type") != "state_changed":
                    continue
                payload = event.get("data") or {}
                old = (payload.get("old_state") or {}).get("state")
                new = (payload.get("new_state") or {}).get("state")
                entity = payload.get("entity_id")
                seen.append({"entity_id": entity, "old": old, "new": new})
                print(f"          {entity}: {old} -> {new}")

            print(f"         收到 {len(seen)} 条 state_changed 事件")
            if seen:
                ok("事件流", "订阅通路可用")
            else:
                print("  [WARN] 监听窗口内无事件，通路本身正常但需在窗口内触发设备变化")
            return True
    except Exception as exc:  # noqa: BLE001 - 诊断脚本直接暴露错误
        fail("WebSocket", repr(exc))
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="Home Assistant 联调诊断")
    parser.add_argument("--env", default=str(DEFAULT_ENV), help="env 文件路径")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--listen", type=int, default=20, help="事件监听秒数")
    args = parser.parse_args()

    env = load_env(Path(args.env))
    base_url = (args.base_url or env.get("HA_BASE_URL") or "").rstrip("/")
    token = args.token or env.get("HA_TOKEN") or ""
    if not base_url or not token:
        print("缺少 HA_BASE_URL 或 HA_TOKEN，请在 backend/.env 中配置")
        return 2

    print(f"目标: {base_url}")
    print(f"令牌: {mask(token)}")

    rest_ok, client = await check_rest(base_url, token)
    ws_ok = await check_ws(base_url, token, args.listen)
    if client is not None:
        await client.aclose()

    print("\n[汇总]")
    print(f"  REST: {'通过' if rest_ok else '失败'}")
    print(f"  WS  : {'通过' if ws_ok else '失败'}")
    return 0 if (rest_ok and ws_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
