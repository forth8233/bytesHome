"""一次性验证脚本：向飞书推送一条真实的「待确认提案」卡片消息。

走真实生产路径：读库里的飞书配置（解密后仅内存使用，不打印）→
ChannelNotifier 同款 push 链路（identity.meta_json 里的 chat_id）→
render_proposal_push 渲染 → lark_oapi HTTP API 发送。
只发消息，不创建提案记录、不执行任何设备动作、不启动 WebSocket 长连接
（避免和运行中的后端抢事件）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import lark_oapi as lark
from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
from lark_oapi.api.im.v1.model.create_message_request_body import (
    CreateMessageRequestBody,
)

from app.channels.render import render_proposal_push
from app.db.session import SessionFactory
from app.services.config_service import ConfigService

FEISHU_KEY = "channels.feishu"


async def main() -> int:
    config = ConfigService(SessionFactory)
    await config.load()

    instances = config.get(FEISHU_KEY) or []
    if isinstance(instances, dict):  # 旧格式单应用
        instances = [instances]
    active = []
    for index, item in enumerate(instances, start=1):
        if (
            isinstance(item, dict)
            and item.get("enabled")
            and str(item.get("app_id") or "").strip()
        ):
            # 名字规则与 ChannelManager 一致：行内名字优先，否则按位置补（feishu / feishu_2 …）
            import re

            name = str(item.get("name") or "").strip()
            if not re.fullmatch(r"feishu(?:_\d+)?", name):
                name = "feishu" if index == 1 else f"feishu_{index}"
            active.append({**item, "name": name})
    if not active:
        print("没有已启用的飞书应用配置，先在配置页保存。")
        return 1

    from sqlalchemy import select

    from app.db.models import ChannelIdentity

    async with SessionFactory() as session:
        identities = (
            await session.execute(
                select(ChannelIdentity).where(
                    ChannelIdentity.enabled.is_(True),
                    ChannelIdentity.channel.in_(
                        [str(item.get("name") or "") for item in active]
                    ),
                )
            )
        ).scalars().all()
    if not identities:
        print("没有已绑定的飞书渠道账号（channel_identity 为空）。")
        return 1

    # 测试用提案：纯展示内容，不落库、不执行
    proposal = {
        "id": 0,  # 0 表示这是链路测试，不是真实提案编号
        "risk_level": "medium",
        "entity_id": "light.living_room",
        "suggestion": "链路测试：检测到客厅灯整夜未关，建议定时 23:30 自动关闭。",
        "actions": [
            {
                "entity_id": "light.living_room",
                "domain": "light",
                "service": "turn_off",
                "data": {},
            }
        ],
    }
    message = render_proposal_push(proposal)

    sent = 0
    for item in active:
        name = str(item.get("name") or "")
        api = (
            lark.Client.builder()
            .app_id(str(item.get("app_id")))
            .app_secret(str(item.get("app_secret")))
            .build()
        )
        for identity in identities:
            if identity.channel != name:
                continue
            try:
                meta = json.loads(identity.meta_json or "{}")
            except (TypeError, ValueError):
                meta = {}
            chat_id = str(meta.get("chat_id") or identity.external_id)
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(json.dumps(message.card, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = await asyncio.get_running_loop().run_in_executor(
                None, api.im.v1.message.create, request
            )
            code = getattr(response, "code", None)
            if code in (0, None):
                sent += 1
                print(f"[{name}] 卡片已发送 → chat_id={chat_id[:12]}…")
            else:
                print(
                    f"[{name}] 发送被拒：code={code} msg={getattr(response, 'msg', '')}"
                )

    print(f"完成：成功 {sent} 条。这条消息不含可执行动作，回复「确认 #0」不会有任何效果。")
    return 0 if sent else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
