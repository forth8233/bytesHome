"""只读探针：直接用线上词库 + 实时 HA 状态跑 VoiceParser.parse，不经过 HTTP。

不执行任何设备动作，不打印任何密钥。
用法：python tools/probe_voice_parse.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db.models import EntityAlias, VoiceGroupList, VoiceLexiconEntry  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402
from app.fastpath.entities import EntityResolver  # noqa: E402
from app.fastpath.voice import VoiceParser, VoiceLexicon  # noqa: E402

TEXTS = [
    "关筒灯",
    "现在有哪些灯亮着",
    "筒灯是亮着的吗",
    "打开筒灯",
    "把客厅灯关掉",
    "关客厅灯",
]


class FakeMember:
    def __init__(self, role: str) -> None:
        self.role = role


async def main() -> int:
    async with SessionFactory() as session:
        voice_rows = (
            await session.scalars(
                select(VoiceLexiconEntry).order_by(VoiceLexiconEntry.id)
            )
        ).all()
        alias_rows = (await session.scalars(select(EntityAlias))).all()
        group_rows = (
            await session.scalars(select(VoiceGroupList).where(VoiceGroupList.enabled.is_(True)))
        ).all()

    print(f"lexicon entries: {len(voice_rows)}, aliases: {len(alias_rows)}, groups: {len(group_rows)}")

    entries = [
        {
            "lexicon": row.lexicon,
            "term": row.term or "",
            "canonical": row.canonical or "",
            "alias": row.alias,
            "functions": (),
            "unit": row.unit or "",
            "numeric_value": row.numeric_value,
            "entry_type": row.entry_type,
            "source": row.source or "xlsx",
        }
        for row in voice_rows
    ]

    from app.fastpath.engine import LexiconEntry

    lexicon = VoiceLexicon.from_entries(
        [
            LexiconEntry(
                lexicon=e["lexicon"],
                term=e["term"],
                canonical=e["canonical"],
                alias=e["alias"],
                functions=e["functions"],
                unit=e["unit"],
                numeric_value=e["numeric_value"],
                entry_type=e["entry_type"],
                source=e["source"],
            )
            for e in entries
        ]
    )
    aliases = [
        {
            "entity_id": row.entity_id,
            "alias": row.alias,
            "display_name": row.display_name,
            "area_id": row.area_id,
            "domain": row.domain or row.entity_id.split(".", 1)[0],
            "can_control": row.can_control,
        }
        for row in alias_rows
    ]

    from app.ha.client_rest import HARestClient
    from app.core.config import get_settings

    settings_obj = get_settings()
    client = HARestClient(settings_obj.ha_base_url, settings_obj.ha_token)
    try:
        raw = await client.get_states()
    finally:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result
    states = {item["entity_id"]: item for item in raw}
    print(f"ha states: {len(states)}")

    parser = VoiceParser(lexicon, aliases, None, {}, [])
    resolver = EntityResolver(aliases, None)

    for role, patterns in (("admin", None), ("member", []), ("member", ["light.*"])):
        print(f"\n=== {role} view_patterns={patterns} ===")
        for text in TEXTS:
            result = parser.parse(
                text,
                states=states,
                resolver=resolver,
                member=FakeMember(role),
                view_patterns=patterns,
                settings={"voice_max_group_actions": 50},
            )
            if result is None:
                print(f"  {text}: None (voice_no_match)")
                continue
            print(
                f"  {text}: status={result.status} reply={result.reply!r} "
                f"actions={[a.get('entity_id') for a in (result.actions or [])][:4]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
