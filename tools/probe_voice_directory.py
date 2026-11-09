"""只读探针：通过运行中的后端读取语音设备目录（含实时 HA 名称），不执行任何设备动作。"""

from __future__ import annotations

import json

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "66668888"},
        )
        login.raise_for_status()
        token = login.json()["access_token"]
        data = client.get(
            "/api/fastpath/voice/directory",
            headers={"Authorization": f"Bearer {token}"},
        ).json()

    entities = data.get("entities") or data.get("devices") or []
    print("keys:", sorted(data.keys()))
    print("count:", len(entities))
    lights = [item for item in entities if item.get("device_type") == "light"]
    print("lights:", len(lights))
    for item in sorted(lights, key=lambda x: (x.get("area_id") or "", x.get("name") or "")):
        print(
            json.dumps(
                {
                    "entity_id": item.get("entity_id"),
                    "name": item.get("name"),
                    "area_id": item.get("area_id"),
                },
                ensure_ascii=False,
            )
        )
    print("areas:", json.dumps(data.get("areas"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
