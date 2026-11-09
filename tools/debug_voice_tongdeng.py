"""只读调试：追踪「关筒灯」在 VoiceParser 里走到哪一步断掉。不执行任何设备动作。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db.models import EntityAlias, VoiceGroupList, VoiceLexiconEntry  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402
from app.fastpath.entities import EntityResolver  # noqa: E402
from app.fastpath.normalize import normalize as normalize_voice  # noqa: E402
from app.fastpath.voice import VoiceLexicon, VoiceParser, DOMAIN_TYPE_HINTS  # noqa: E402


class FakeMember:
    role = "admin"


async def main() -> int:
    async with SessionFactory() as session:
        voice_rows = (await session.scalars(select(VoiceLexiconEntry))).all()
        alias_rows = (await session.scalars(select(EntityAlias))).all()
        group_rows = (
            await session.scalars(select(VoiceGroupList).where(VoiceGroupList.enabled.is_(True)))
        ).all()

    from app.fastpath.engine import LexiconEntry

    lexicon = VoiceLexicon.from_entries(
        [
            LexiconEntry(
                lexicon=row.lexicon,
                term=row.term or "",
                canonical=row.canonical or "",
                alias=row.alias,
                functions=(),
                unit=row.unit or "",
                numeric_value=row.numeric_value,
                entry_type=row.entry_type,
                source=row.source or "xlsx",
            )
            for row in voice_rows
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

    s = get_settings()
    client = HARestClient(s.ha_base_url, s.ha_token)
    try:
        raw = await client.get_states()
    finally:
        await client.close()
    states = {item["entity_id"]: item for item in raw}

    parser = VoiceParser(lexicon, aliases, None, {}, [])
    resolver = EntityResolver(aliases, None)

    for text in ("关筒灯", "打开筒灯", "现在有哪些灯亮着", "筒灯是亮着的吗"):
        norm = normalize_voice(text)
        print(f"\n=== {text} -> {norm} ===")
        print("  device_types contains 灯:", "灯" in lexicon.device_types, lexicon.device_types.get("灯"))
        print("  enums contains 关:", "关" in lexicon.enums, lexicon.enums.get("关"))
        print("  enums contains 打开:", lexicon.enums.get("打开"))
        print("  functions contains 状态:", lexicon.functions.get("状态"))
        print("  query_words sample:", list(lexicon.query_words)[:12])
        # 名字里有没有和文本有交集的
        hits = [
            key
            for key in parser._names
            if key and (key in norm or norm in key)
        ][:10]
        print("  name overlaps:", hits)
        type_hits = [k for k in list(lexicon.device_types) + list(DOMAIN_TYPE_HINTS) if k in norm]
        print("  type overlaps:", type_hits)
        result = parser.parse(
            text,
            states=states,
            resolver=resolver,
            member=FakeMember(),
            view_patterns=None,
            settings={"voice_max_group_actions": 50},
        )
        print("  result:", None if result is None else (result.status, result.reply))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
