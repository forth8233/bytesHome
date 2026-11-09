#!/usr/bin/env python3
"""Import the smart-home voice lexicon workbook into the database.

Usage:
    backend/.venv/Scripts/python.exe tools/import_voice_lexicon.py <workbook.xlsx> [--force]

Run from the repository root. The script changes into backend/ so the database
URL and .env are resolved exactly as the server resolves them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the voice lexicon workbook")
    parser.add_argument("workbook", nargs="?", help="path to the xlsx file")
    parser.add_argument("--force", action="store_true", help="re-import the same sha256")
    parser.add_argument(
        "--seed-only", action="store_true", help="only insert the PDF seed entries"
    )
    args = parser.parse_args()

    if not args.seed_only and not args.workbook:
        parser.error("workbook is required unless --seed-only is used")

    workbook = Path(args.workbook) if args.workbook else None
    if workbook is not None:
        if not workbook.is_absolute():
            workbook = (Path.cwd() / workbook).resolve()
        if not workbook.exists():
            print(f"workbook not found: {workbook}", file=sys.stderr)
            return 1

    os.chdir(ROOT / "backend")
    sys.path.insert(0, str(ROOT / "backend"))

    from app.db.session import SessionFactory, init_db
    from app.fastpath.voice_import import import_workbook, seed_pdf_entries

    async def run() -> dict:
        await init_db()
        if args.seed_only:
            async with SessionFactory() as session:
                inserted = await seed_pdf_entries(session)
                await session.commit()
            return {"imported": True, "seeded": inserted}
        return await import_workbook(
            workbook, session_factory=SessionFactory, force=args.force
        )

    summary = asyncio.run(run())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.seed_only:
        print(
            f"\nimported={summary.get('imported')} duplicate={summary.get('duplicate', False)} "
            f"entries={summary.get('entries', 0)} errors={summary.get('error_count', 0)}"
        )
    return 0 if summary.get("imported") or summary.get("duplicate") else 2


if __name__ == "__main__":
    raise SystemExit(main())
