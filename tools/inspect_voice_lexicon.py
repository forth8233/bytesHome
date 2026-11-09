#!/usr/bin/env python3
"""Inspect the smart-home voice lexicon workbook structure.

Usage:
    backend/.venv/Scripts/python.exe tools/inspect_voice_lexicon.py <workbook.xlsx>
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

from openpyxl import load_workbook


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    workbook = load_workbook(sys.argv[1], read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows)
    print("sheet:", sheet.title)
    print("header:", header)

    counts: Counter[str] = Counter()
    samples: dict[str, list[tuple]] = defaultdict(list)
    grouped: dict[str, list[tuple]] = defaultdict(list)
    max_alias = 0
    for row in rows:
        if not row or not row[0]:
            continue
        lexicon = str(row[0]).strip()
        counts[lexicon] += 1
        grouped[lexicon].append(tuple(row))
        if len(samples[lexicon]) < 4:
            samples[lexicon].append(tuple(row))
        for value in row:
            if value is not None:
                max_alias = max(max_alias, len(str(value)))

    print("total:", sum(counts.values()), "lexicons:", len(counts))
    print("max cell length:", max_alias)
    for lexicon, count in counts.most_common():
        print(f"\n[{lexicon}] {count}")
        for sample in samples[lexicon]:
            print("  ", sample)

    for name in ("相对调低", "相对调高"):
        base = [row for row in grouped[name] if row[3] in (None, "")]
        valued = [row for row in grouped[name] if row[3] not in (None, "")]
        print(f"\n[{name}] rows={counts[name]} base={len(base)} valued={len(valued)}")
        print("  base terms:", [row[1] for row in base])
        for row in valued[:6]:
            print("  valued:", row)

    for name in ("功能名称", "单位", "设备类型", "查询", "一点"):
        print(f"\n[{name}] all rows:")
        for row in grouped[name]:
            print("  ", row)

    for name in ("属性数值", "枚举值"):
        functions = Counter(
            str(row[2]) for row in grouped[name] if row[2] not in (None, "")
        )
        print(f"\n[{name}] functions:", functions)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
