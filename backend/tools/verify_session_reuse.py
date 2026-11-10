"""验证同一飞书渠道的消息会复用同一会话（problem 2）。

两次对真实 ChatService.chat() 调用，使用完全相同的 (channel, channel_ref)，
且第二次不传 session_id（模拟服务重启后 dispatcher 内存 _sessions 为空、
首条语音无已知会话）。断言两次归属到同一个 ChatSession 行，而不是新行。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(ROOT/'data'/'verify_reuse.db').as_posix()}"
os.environ["AUTO_CREATE_SCHEMA"] = "true"
os.environ["INITIAL_ADMIN_PASSWORD"] = "verify-secret"
os.environ["APP_SECRET_KEY"] = "verify-key"
os.environ["APP_ENCRYPTION_KEY"] = ""
os.environ["HA_BASE_URL"] = "http://127.0.0.1:1"
os.environ["HA_TOKEN"] = ""

from sqlalchemy import delete, select  # noqa: E402

from app.agent.proposals import ProposalService  # noqa: E402
from app.chat.service import ChatService  # noqa: E402
from app.db.models import ChatSession, Member  # noqa: E402
from app.db.session import SessionFactory, init_db, engine  # noqa: E402
from app.llm.client import LLMResult  # noqa: E402
from app.safety.guard import ActionGuard  # noqa: E402
from app.safety.whitelist import WhitelistRules  # noqa: E402


class ChatConfig:
    def global_settings(self) -> dict:
        return {
            "proposal_timeout_seconds": 900,
            "proposal_dedupe_seconds": 600,
            "feedback_reject_threshold": 2,
            "feedback_cooldown_seconds": 3600,
        }

    def whitelist(self) -> WhitelistRules:
        return WhitelistRules(
            entity_allow=("light.*", "lock.*"),
            service_allow=("light.turn_on", "light.turn_off"),
        )


class StubAdapter:
    """ChatService 只读/空控制走完即可，不必真的调 HA。"""

    async def call_service(self, domain, service, data):
        return []

    async def get_state(self, entity_id):
        return None


class StubProposals:
    async def get(self, proposal_id):
        raise FileNotFoundError


class FakeLLM:
    async def structured_with_fallback(self, messages, model_cls, **kwargs):
        return LLMResult(
            ok=True,
            parsed_ok=True,
            provider="stub",
            model="stub",
            content="{}",
            data={"reply": "好的。", "tool_calls": []},
            duration_ms=1,
        )


async def main() -> int:
    import app.db.models  # noqa: F401  (register mappers)
    await init_db()

    async with SessionFactory() as session:
        await session.execute(delete(ChatSession))
        await session.execute(delete(Member))
        member = Member(
            username="verify_admin",
            password_hash="x",
            role="admin",
            enabled=True,
        )
        session.add(member)
        await session.commit()
        await session.refresh(member)
        member_pk = member.id
        print(f"[setup] Member id = {member_pk}")

    config = ChatConfig()
    guard = ActionGuard(config, session_factory=SessionFactory)
    proposals = ProposalService(
        session_factory=SessionFactory, config=config, adapter=StubAdapter(), guard=guard
    )
    adapter = StubAdapter()
    chat = ChatService(
        session_factory=SessionFactory,
        config=config,
        adapter=adapter,
        guard=guard,
        proposals=proposals,
        client=FakeLLM(),
    )

    member = SimpleNamespace(id=member_pk, username="verify_admin", role="admin")
    CH = "feishu_2"
    REF = "oc_verify_group"

    # 第一次：无已知会话 → 应新建
    r1 = await chat.chat(member, "一分钟之后关灯", None, channel=CH, channel_ref=REF)
    # 第二次：同渠道，仍不传 session_id（模拟重启后）→ 应复用
    r2 = await chat.chat(member, "一分钟之后开灯", None, channel=CH, channel_ref=REF)

    async with SessionFactory() as session:
        rows = (await session.scalars(select(ChatSession))).all()

    print(f"第一次 session_id = {r1.get('session_id')}")
    print(f"第二次 session_id = {r2.get('session_id')}")
    print(f"DB 中 ChatSession 行数 = {len(rows)}")

    same_session = r1.get("session_id") == r2.get("session_id")
    single_row = len(rows) == 1
    ok = same_session and single_row
    print("RESULT:", "PASS (reused same session)" if ok else "FAIL (duplicate sessions)")
    await engine.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))