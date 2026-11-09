"""只读诊断：检查快路径语音解析对若干句话的结果。

用法：cd backend && ./.venv/Scripts/python.exe ../tools/probe_voice.py "色温灯调暖些" ...
不执行任何设备动作，不打印令牌。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402
from sqlalchemy import or_, select  # noqa: E402

from app.core.security import create_access_token  # noqa: E402
from app.db.models import EntityAlias, Member, VoiceLexiconEntry  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402


async def admin_id() -> int:
    async with SessionFactory() as session:
        row = await session.scalar(select(Member).where(Member.username == "admin"))
        if row is None:
            raise SystemExit("admin member missing")
        return row.id


async def lookup_aliases(keyword: str) -> None:
    async with SessionFactory() as session:
        rows = (
            await session.scalars(
                select(EntityAlias)
                .where(
                    or_(
                        EntityAlias.alias.contains(keyword),
                        EntityAlias.display_name.contains(keyword),
                    )
                )
                .order_by(EntityAlias.id)
                .limit(20)
            )
        ).all()
        print(f"[aliases containing {keyword!r}] {len(rows)}")
        for row in rows:
            print(
                f"  {row.entity_id} alias={row.alias!r} name={row.display_name!r} "
                f"area={row.area_id!r} can_control={row.can_control}"
            )


async def lookup_lexicon(*terms: str) -> None:
    async with SessionFactory() as session:
        for term in terms:
            rows = (
                await session.scalars(
                    select(VoiceLexiconEntry)
                    .where(VoiceLexiconEntry.alias == term)
                    .order_by(VoiceLexiconEntry.id)
                    .limit(20)
                )
            ).all()
            print(f"[lexicon alias {term!r}] {len(rows)}")
            for row in rows:
                print(
                    f"  lexicon={row.lexicon} term={row.term!r} canonical={row.canonical!r} "
                    f"functions={row.functions_json} unit={row.unit!r} "
                    f"numeric={row.numeric_value} source={row.source} enabled={row.enabled}"
                )


async def probe(phrase: str, token: str) -> None:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=20) as client:
        response = await client.post(
            "/api/fastpath/voice/test",
            json={"text": phrase},
            headers={"Authorization": f"Bearer {token}"},
        )
    print(f"[{phrase}] {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        print(response.text[:500])
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


async def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    phrases = sys.argv[1:] or ["色温灯调暖些"]
    await lookup_aliases("色温")
    await lookup_lexicon("暖", "调暖", "暖一点", "些", "一点", "一点点")
    token, _, _ = create_access_token(await admin_id())
    for phrase in phrases:
        await probe(phrase, token)


if __name__ == "__main__":
    asyncio.run(main())
