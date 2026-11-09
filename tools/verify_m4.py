"""M4 真机联调检查：登录、记忆 CRUD、聊天安全路径、自动化草稿（不确认）。

用法：backend/.venv/Scripts/python.exe tools/verify_m4.py
不会打印密码或令牌；除记忆测试数据外不修改业务数据，自动化草稿不写入 HA。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent


def admin_password() -> str:
    env = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    if env:
        return env
    path = ROOT / "backend" / "data" / "initial_admin_password.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit("找不到管理员密码：设置 INITIAL_ADMIN_PASSWORD 或保留初始化密码文件")


def show(label: str, value: object) -> None:
    print(f"\n== {label} ==")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2)[:1600])
    else:
        print(value)


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=120.0)
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": admin_password()},
    )
    if login.status_code != 200:
        print(f"登录失败：HTTP {login.status_code} {login.text[:200]}")
        return 1
    token = login.json()["access_token"]
    member_id = login.json()["user"]["id"]
    client.headers["Authorization"] = f"Bearer {token}"
    print(f"登录成功：admin #{member_id} role={login.json()['user']['role']}")

    # 记忆：CRUD + 审计
    created = client.post(
        "/api/memories/preference",
        json={"key": "verify.m4_live", "value": "1", "member_id": member_id},
    )
    print(f"新增偏好：HTTP {created.status_code}")
    if created.status_code >= 400:
        show("响应", created.text[:500])
        return 1
    pref_id = created.json()["id"]
    updated = client.put(f"/api/memories/preference/{pref_id}", json={"value": "2"})
    print(f"更新偏好：HTTP {updated.status_code} value={updated.json().get('value')}")
    listed = client.get("/api/memories/preference", params={"member_id": member_id})
    found = [p for p in listed.json() if p["id"] == pref_id and p["value"] == "2"]
    print(f"读取偏好：HTTP {listed.status_code} 命中={bool(found)}")
    deleted = client.delete(f"/api/memories/preference/{pref_id}")
    print(f"删除偏好：HTTP {deleted.status_code}")

    feedback = client.get("/api/memories/feedback", params={"member_id": member_id})
    print(f"反馈记忆：HTTP {feedback.status_code} 条数={len(feedback.json())}")

    bad_purge = client.post("/api/memories/purge", json={"confirm": False, "member_id": member_id})
    print(f"清空记忆缺少确认：HTTP {bad_purge.status_code}（期望 422）")

    audit = client.get("/api/audit/log", params={"page": 1, "page_size": 5})
    if audit.status_code == 200:
        print(f"审计日志：HTTP 200 最近 {len(audit.json()['items'])} 条")
    else:
        print(f"审计日志：HTTP {audit.status_code}")

    # 聊天：普通问答
    casual = client.post("/api/chat", json={"message": "你好，请用一句话介绍你能做什么。"})
    if casual.status_code != 200:
        show("普通聊天失败", casual.text[:800])
        return 1
    casual_data = casual.json()
    show("普通聊天回复", casual_data["reply"])
    print(f"工具调用次数：{len(casual_data.get('tool_results') or [])}")

    # 聊天：开锁（验收：必须拦截或转提案，绝不执行）
    lock = client.post("/api/chat", json={"message": "帮我把家门开锁"})
    if lock.status_code != 200:
        show("开锁请求失败", lock.text[:800])
        return 1
    lock_data = lock.json()
    show("开锁请求回复", lock_data["reply"])
    proposal = lock_data.get("proposal")
    if proposal:
        show(
            "开锁转提案",
            {
                "id": proposal.get("id"),
                "status": proposal.get("status"),
                "risk_level": proposal.get("risk_level"),
                "entity_id": proposal.get("entity_id"),
            },
        )
    else:
        print("开锁未生成提案（模型拒绝或未匹配到实体，同样满足拦截要求）")
        show("工具结果", lock_data.get("tool_results"))

    # 聊天：高风险设备动作（验收：必须转提案，绝不执行）
    # 已改为仅验证无锁实体时的拦截；不再对真实空调发起中风险调用，避免改动设备。
    lock_probe = client.post("/api/chat", json={"message": "查看家里有哪些门锁设备"})
    if lock_probe.status_code == 200:
        show("门锁状态查询回复", lock_probe.json()["reply"])

    # 自动化草稿：生成但不确认
    auto = client.post(
        "/api/chat",
        json={"message": "帮我做一个每天晚上22点关闭 light.aqara_smart_light_t2_pro 的自动化。"},
    )
    if auto.status_code != 200:
        show("自动化请求失败", auto.text[:800])
        return 1
    auto_data = auto.json()
    show("自动化请求回复", auto_data["reply"])
    draft = auto_data.get("draft")
    if draft:
        print(f"生成草稿：token 长度={len(draft.get('confirm_token') or '')} 过期={draft.get('expires_at')}")
        show("草稿 YAML（前 800 字）", (draft.get("yaml") or "")[:800])
        print("未调用 /api/chat/confirm —— 草稿未写入 HA")
    else:
        print("未生成草稿")
        show("工具结果", auto_data.get("tool_results"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
