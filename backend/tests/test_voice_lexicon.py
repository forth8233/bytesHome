from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import delete, select

from app.db.models import VoiceImportBatch, VoiceLexiconEntry
from app.db.session import SessionFactory
from app.fastpath.voice_import import import_workbook, load_seed_entries
from app.fastpath.voice_lexicon import (
    LEXICONS,
    parse_number,
    parse_row,
    parse_workbook,
    split_aliases,
)

WORKBOOK = (
    Path(__file__).resolve().parents[2]
    / "智能家居语音技能"
    / "语音控制技能_系统词库_汇总版20250208170728.xlsx"
)
HEADER = ("词库名称", "词条名", "推断功能", "标识符", "别名", "词条类型")


def test_parse_number() -> None:
    assert parse_number("90%") == 90
    assert parse_number("负2") == -2
    assert parse_number("-3.5") == -3.5
    assert parse_number(5.0) == 5
    assert parse_number("亮度") is None
    assert parse_number("") is None


def test_split_aliases() -> None:
    assert split_aliases("1楼/一层、底楼") == ["1楼", "一层", "底楼"]
    assert split_aliases("") == []


def test_parse_row_unit_normalizes_model() -> None:
    entries = parse_row(("单位", "档", "speed", "model", "档位/风速档", 1))
    assert [entry.alias for entry in entries] == ["档", "档位", "风速档"]
    assert entries[0].canonical == "档"
    assert entries[0].unit == "档"
    assert entries[0].functions == ("speed",)


def test_parse_row_relative_value() -> None:
    entries = parse_row(("相对调高", "大一点", "brightness", "20", "大一点/高一些", 1))
    assert entries[0].canonical == "20"
    assert entries[0].numeric_value == 20
    assert entries[0].functions == ("brightness",)
    assert entries[0].entry_type == 1


def test_parse_row_custom_type() -> None:
    entries = parse_row(("查询", "查查看", "", "", "", 2))
    assert entries[0].entry_type == 2
    assert entries[0].canonical == "query"


def test_parse_row_rejects_unknown_lexicon() -> None:
    with pytest.raises(ValueError):
        parse_row(("未知词库", "测试", "", "", "", 1))
    with pytest.raises(ValueError):
        parse_row(("单位", "", "", "", "", 1))


@pytest.mark.skipif(not WORKBOOK.exists(), reason="voice lexicon workbook missing")
def test_parse_real_workbook() -> None:
    parsed = parse_workbook(WORKBOOK)
    assert parsed.total_rows == 38917
    assert parsed.error_count == 0
    assert set(parsed.source_rows) == set(LEXICONS)
    assert parsed.source_rows["increase"] == 12636
    assert parsed.source_rows["decrease"] == 14731
    assert parsed.source_rows["sceneName"] == 58
    assert parsed.source_rows["deviceType"] == 16
    assert set(parsed.alias_rows) == set(LEXICONS)
    assert len(parsed.entries) == 128094

    def base(lexicon: str) -> list:
        return [
            entry
            for entry in parsed.entries
            if entry.lexicon == lexicon and entry.numeric_value is None
        ]

    assert len(base("increase")) == 445
    assert len(base("decrease")) == 508
    aliases = {entry.alias for entry in parsed.entries if entry.lexicon == "increase"}
    assert {"调大", "大一点", "大百分之壹"} <= aliases
    units = {
        entry.alias: (entry.canonical, entry.functions)
        for entry in parsed.entries
        if entry.lexicon == "unit"
    }
    assert units["档"][0] == "档"
    assert units["度"] == ("°", ("targetTemp", "angle"))
    assert units["K值"] == ("K", ("colorTemp",))


def test_seed_entries() -> None:
    entries = load_seed_entries()
    assert len(entries) == 37
    assert all(entry.source == "pdf" for entry in entries)
    stems = {entry.alias for entry in entries if entry.lexicon == "increase"}
    assert {"大", "亮", "升"} <= stems
    assert {entry.alias for entry in entries if entry.lexicon == "little"} >= {"一点点"}


def _write_workbook(path: Path, rows: list[tuple]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    workbook.save(path)


async def _cleanup() -> None:
    async with SessionFactory() as session:
        await session.execute(
            delete(VoiceLexiconEntry).where(VoiceLexiconEntry.alias.like("测%"))
        )
        await session.execute(
            delete(VoiceLexiconEntry).where(
                VoiceLexiconEntry.alias.in_(["大", "一点点"])
            )
        )
        await session.execute(
            delete(VoiceImportBatch).where(
                VoiceImportBatch.filename.like("voice-test%")
            )
        )
        await session.commit()

@pytest.fixture()
def small_workbook(tmp_path: Path) -> Path:
    path = tmp_path / "voice-test.xlsx"
    _write_workbook(
        path,
        [
            ("相对调高", "大", "brightness", "5", "大", 1),
            ("单位", "测单位", "brightness", "model", "测单位/测单", 1),
            ("查询", "测查询", "", "", "测查询", 2),
        ],
    )
    return path


async def test_import_upserts_and_detects_duplicate(
    client, small_workbook: Path
) -> None:
    await _cleanup()
    try:
        first = await import_workbook(small_workbook, session_factory=SessionFactory)
        assert first["imported"] is True
        assert first["source_rows"] == {"increase": 1, "unit": 1, "query": 1}
        assert first["error_count"] == 0

        async with SessionFactory() as session:
            big = (
                await session.execute(
                    select(VoiceLexiconEntry).where(
                        VoiceLexiconEntry.lexicon == "increase",
                        VoiceLexiconEntry.alias == "大",
                    )
                )
            ).scalar_one()
            assert big.canonical == "5"
            assert big.source == "xlsx"
            seeded = (
                await session.execute(
                    select(VoiceLexiconEntry).where(
                        VoiceLexiconEntry.lexicon == "little",
                        VoiceLexiconEntry.alias == "一点点",
                    )
                )
            ).scalar_one()
            assert seeded.source == "pdf"

        second = await import_workbook(small_workbook, session_factory=SessionFactory)
        assert second["imported"] is False
        assert second["duplicate"] is True

        forced = await import_workbook(
            small_workbook, session_factory=SessionFactory, force=True
        )
        assert forced["imported"] is True
        assert forced["duplicate"] is True
    finally:
        await _cleanup()


async def test_import_does_not_overwrite_custom_entry(
    client, small_workbook: Path
) -> None:
    await _cleanup()
    try:
        async with SessionFactory() as session:
            session.add(
                VoiceLexiconEntry(
                    lexicon="increase",
                    term="大",
                    canonical="custom",
                    alias="大",
                    functions_json="[]",
                    unit="",
                    numeric_value=None,
                    entry_type=2,
                    source="manual",
                )
            )
            await session.commit()

        await import_workbook(small_workbook, session_factory=SessionFactory)

        async with SessionFactory() as session:
            entry = (
                await session.execute(
                    select(VoiceLexiconEntry).where(
                        VoiceLexiconEntry.lexicon == "increase",
                        VoiceLexiconEntry.alias == "大",
                    )
                )
            ).scalar_one()
            assert entry.canonical == "custom"
            assert entry.entry_type == 2
            assert entry.source == "manual"
    finally:
        await _cleanup()
