"""只读探针：以指定成员身份走 /api/fastpath/test，看解析结果。

不改任何配置、不执行任何设备动作。
用法：python tools/probe_fastpath_hzj.py <member_id>
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"
TEXTS = [
    "关筒灯",
    "现在有哪些灯亮着",
    "筒灯是亮着的吗",
    "打开筒灯",
    "客厅温度",
]


def main() -> int:
    member_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "66668888"},
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        for text in TEXTS:
            response = client.post(
                "/api/fastpath/test",
                headers=headers,
                json={"text": text, "member_id": member_id},
            )
            data = response.json()
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                result = data
            print(
                json.dumps(
                    {
                        "text": text,
                        "status": result.get("status"),
                        "reply": result.get("reply"),
                        "actions": [
                            a.get("entity_id") for a in (result.get("actions") or [])
                        ],
                    },
                    ensure_ascii=False,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
