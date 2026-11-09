"""只读探针：走运行中后端的 /api/fastpath/test，打印语音解析全量结果。不执行设备动作。"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"
TEXTS = sys.argv[1:] or [
    "关筒灯",
    "打开筒灯",
    "现在有哪些灯亮着",
    "筒灯是亮着的吗",
    "关客厅灯",
    "关闭走廊筒灯1",
    "走廊筒灯1是亮着的吗",
]


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "66668888"},
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        for text in TEXTS:
            data = client.post("/api/fastpath/test", headers=headers, json={"text": text}).json()
            print("=" * 60)
            print("TEXT:", text)
            print("route:", data.get("route"))
            print("voice:", json.dumps(data.get("voice"), ensure_ascii=False))
            print("scene_rule:", json.dumps(data.get("scene_rule"), ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
