"""只读探针：查词库里与「开关灯」相关的词条，确认哪些字能被识别。不执行任何设备动作。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db.models import VoiceLexiconEntry  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402

NEEDLES = ("关", "开", "亮", "暗", "有", "哪些", "筒", "灯带", "着")


async def main() -> int:
    async with SessionFactory() as session:
        rows = (await session.scalars(select(VoiceLexiconEntry))).all()

    print(f"total entries: {len(rows)}")
    by_lexicon: dict[str, int] = {}
    for row in rows:
        by_lexicon[row.lexicon] = by_lexicon.get(row.lexicon, 0) + 1
    print("by lexicon:", by_lexicon)

    for needle in NEEDLES:
        hits = [
            (row.lexicon, row.term, row.canonical, row.entry_type)
            for row in rows
            if row.term and needle in row.term
        ]
        exact = [hit for hit in hits if hit[1] == needle]
        print(f"\n=== {needle!r} total={len(hits)} exact={len(exact)} ===")
        for hit in exact[:8]:
            print("  exact:", hit)
        for hit in hits[:12]:
            print("  ", hit)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
