"""只读查看 HA 当前空调状态与最近历史，用于确认联调是否改动了设备。

用法：backend/.venv/Scripts/python.exe tools/ha_read_state.py
只发 GET 请求，不调用任何服务。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / "backend" / ".env"
    if not path.exists():
        raise SystemExit("backend/.env 不存在")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = load_env()
    base = env["HA_BASE_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {env['HA_TOKEN']}"}
    with httpx.Client(base_url=base, headers=headers, timeout=30.0) as client:
        states = client.get("/api/states").json()
        climates = [s for s in states if s["entity_id"].startswith("climate.")]
        print("当前空调实体：")
        for item in climates:
            attrs = item.get("attributes", {})
            print(
                f"- {item['entity_id']} | {attrs.get('friendly_name')} | "
                f"state={item['state']} | target={attrs.get('temperature')} | "
                f"current={attrs.get('current_temperature')} | "
                f"mode={attrs.get('hvac_mode') or item['state']}"
            )

        start = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        for item in climates:
            history = client.get(
                f"/api/history/period/{start}",
                params={
                    "filter_entity_id": item["entity_id"],
                    "minimal_response": "",
                    "no_attributes": "",
                },
            )
            if history.status_code != 200:
                print(f"历史查询失败 {item['entity_id']}：HTTP {history.status_code}")
                continue
            rows = history.json()
            samples = rows[0] if rows else []
            print(f"  历史样本 {len(samples)} 条（3 小时）：")
            for sample in samples[-12:]:
                print(
                    f"    {sample.get('last_changed') or sample.get('last_updated')} -> {sample.get('state')}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
