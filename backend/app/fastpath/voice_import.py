from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import VoiceImportBatch, VoiceLexiconEntry, utcnow
from app.fastpath.voice_lexicon import LexiconEntry, parse_workbook

BATCH_SIZE = 100
SEED_PATH = Path(__file__).with_name("voice_seed.json")


def load_seed_entries(path: Path = SEED_PATH) -> list[LexiconEntry]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries: list[LexiconEntry] = []
    for lexicon, body in payload.items():
        for term in body.get("terms", []):
            entries.append(
                LexiconEntry(
                    lexicon=lexicon,
                    term=term,
                    canonical=lexicon,
                    alias=term,
                    entry_type=1,
                    source="pdf",
                )
            )
    return entries


async def seed_pdf_entries(session: AsyncSession) -> int:
    entries = load_seed_entries()
    if not entries:
        return 0
    statement = sqlite_insert(VoiceLexiconEntry).values(
        [_values(entry) for entry in entries]
    )
    statement = statement.on_conflict_do_nothing(
        index_elements=["lexicon", "alias"]
    )
    result = await session.execute(statement)
    return result.rowcount or 0


def _values(entry) -> dict[str, Any]:
    return {
        "lexicon": entry.lexicon,
        "term": entry.term,
        "canonical": entry.canonical,
        "alias": entry.alias,
        "functions_json": json.dumps(list(entry.functions), ensure_ascii=False),
        "unit": entry.unit,
        "numeric_value": entry.numeric_value,
        "entry_type": entry.entry_type,
        "source": entry.source,
        "enabled": True,
    }


async def import_workbook(
    path: str | Path,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    imported_by: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    parsed = parse_workbook(path)
    if not parsed.entries:
        return {
            "imported": False,
            "error": "workbook contains no usable entries",
            "errors": parsed.errors,
            "sha256": parsed.sha256,
        }

    async with session_factory() as session:
        await seed_pdf_entries(session)
        existing = (
            await session.execute(
                select(VoiceImportBatch.id).where(
                    VoiceImportBatch.sha256 == parsed.sha256
                )
            )
        ).scalars().first()
        if existing is not None and not force:
            return {
                "imported": False,
                "duplicate": True,
                "batch_id": existing,
                "sha256": parsed.sha256,
                "source_rows": parsed.source_rows,
                "alias_rows": parsed.alias_rows,
            }

        updated_at = utcnow()
        for start in range(0, len(parsed.entries), BATCH_SIZE):
            chunk = parsed.entries[start : start + BATCH_SIZE]
            statement = sqlite_insert(VoiceLexiconEntry).values(
                [_values(entry) for entry in chunk]
            )
            statement = statement.on_conflict_do_update(
                index_elements=["lexicon", "alias"],
                set_={
                    "term": statement.excluded.term,
                    "canonical": statement.excluded.canonical,
                    "functions_json": statement.excluded.functions_json,
                    "unit": statement.excluded.unit,
                    "numeric_value": statement.excluded.numeric_value,
                    "entry_type": statement.excluded.entry_type,
                    "source": statement.excluded.source,
                    "enabled": statement.excluded.enabled,
                    "updated_at": updated_at,
                },
                where=(VoiceLexiconEntry.entry_type != 2),
            )
            await session.execute(statement)

        batch = VoiceImportBatch(
            filename=Path(path).name,
            sha256=parsed.sha256,
            counts_json=json.dumps(
                {
                    "source_rows": parsed.source_rows,
                    "alias_rows": parsed.alias_rows,
                    "total_rows": parsed.total_rows,
                },
                ensure_ascii=False,
            ),
            errors_json=json.dumps(parsed.errors, ensure_ascii=False),
            imported_by=imported_by,
        )
        session.add(batch)
        await session.commit()
        batch_id = batch.id

    return {
        "imported": True,
        "duplicate": existing is not None,
        "batch_id": batch_id,
        "sha256": parsed.sha256,
        "total_rows": parsed.total_rows,
        "entries": len(parsed.entries),
        "source_rows": parsed.source_rows,
        "alias_rows": parsed.alias_rows,
        "errors": parsed.errors,
        "error_count": parsed.error_count,
    }
