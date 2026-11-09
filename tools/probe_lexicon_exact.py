"""只读探针：打印词库中若干精确词条，确认「关/开/亮/亮着」落在哪个词库。不执行任何设备动作。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db.models import VoiceLexiconEntry  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402

TERMS = ("关", "开", "打开", "关闭", "亮", "亮着", "暗", "灯", "灯光", "筒灯", "哪些", "有")


async def main() -> int:
    async with SessionFactory() as session:
        rows = (await session.scalars(select(VoiceLexiconEntry))).all()

    for term in TERMS:
        hits = [row for row in rows if (row.term or "") == term]
        print(f"=== {term!r} exact={len(hits)} ===")
        for row in hits[:10]:
            print(
                f"  lexicon={row.lexicon} canonical={row.canonical!r} "
                f"term={row.term!r} alias={row.alias!r} "
                f"numeric={row.numeric_value!r} type={row.entry_type}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
