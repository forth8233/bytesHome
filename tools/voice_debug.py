from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.fastpath.entities import EntityResolver  # noqa: E402
from app.fastpath.normalize import normalize_voice  # noqa: E402
from app.fastpath.voice import VoiceLexicon, VoiceParser, VoiceSlots, _Spans, _residue_ok  # noqa: E402
from app.fastpath.voice_import import load_seed_entries  # noqa: E402
from app.fastpath.voice_lexicon import parse_workbook  # noqa: E402
from voice_smoke import ALIASES, STATES, FakeRegistry, WORKBOOK  # noqa: E402

lexicon = VoiceLexicon.from_entries(
    list(parse_workbook(WORKBOOK).entries) + load_seed_entries()
)
parser = VoiceParser(lexicon, ALIASES, FakeRegistry())
resolver = EntityResolver(ALIASES, FakeRegistry())

for phrase in ["把客厅灯调亮一点", "把客厅灯亮度调到50%", "客厅温度多少", "客厅电视音量调到40%"]:
    text = normalize_voice(phrase)
    spans = _Spans(len(text))
    slots = VoiceSlots(original=phrase)
    targets, clarify = parser._target(text, spans, slots, STATES, resolver, None, None)
    print("\n", phrase, "=>", text, "target", slots.target_kind, slots.target_text, "targets", [t.name for t in targets])
    parser._area_and_floor(text, spans, slots)
    print(" area", slots.area_text, "floor", slots.floor_id)
    parser._action_slots(text, spans, slots)
    print(" op", slots.op, "kind", slots.kind, "fn", slots.function, "cand", slots.candidates, "val", slots.value, slots.unit, "dir", slots.direction, "enum", slots.enum)
    leftover = "".join(c for i, c in enumerate(text) if spans.free(i))
    print(" leftover", repr(leftover), "residue_ok", _residue_ok(text, spans))
