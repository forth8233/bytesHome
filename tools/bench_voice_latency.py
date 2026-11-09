"""测量语音词库规模对构建与解析耗时的影响。

两部分：
1. 本地：不同词条规模下 VoiceLexicon.from_entries 的构建耗时（看是否随词库线性增长）。
2. 接口：顺序请求只读的 /api/fastpath/voice/test，测量真实解析耗时（含 HTTP 与配置读取）。

只读，不调用 HA，不执行任何设备动作，也不打印访问令牌。

用法（仓库根目录，后端已在 127.0.0.1:8000 运行）：
    backend/.venv/Scripts/python.exe tools/bench_voice_latency.py
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT / "backend")
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.core.security import create_access_token  # noqa: E402
from app.db.models import Member  # noqa: E402
from app.db.session import SessionFactory  # noqa: E402
from app.fastpath.voice import VoiceLexicon  # noqa: E402
from app.fastpath.voice_import import load_seed_entries  # noqa: E402
from app.fastpath.voice_lexicon import parse_workbook  # noqa: E402

WORKBOOK = (
    ROOT / "智能家居语音技能" / "语音控制技能_系统词库_汇总版20250208170728.xlsx"
)
BASE = os.environ.get("VOICE_API_BASE", "http://127.0.0.1:8000").rstrip("/")

PHRASES = [
    "打开客厅灯",
    "把客厅灯亮度调到50%",
    "客厅灯亮度调到百分之三十",
    "把客厅灯色温调到4000K",
    "客厅灯调到最亮",
    "把客厅灯亮度降低百分之二十",
    "空调温度调高2度",
    "客厅空调风速调低一档",
    "把客厅窗帘打开",
    "窗帘打开一半",
    "客厅电视音量调到40%",
    "客厅电视切换蓝牙",
    "全屋灯光调到50%",
    "关闭客厅所有灯",
    "查询客厅灯的状态",
    "客厅灯现在多亮",
    "客厅空调温度多少",
    "客厅调亮一点",
    "把客厅灯打开然后关闭卧室灯",
]


def build_time(entries, rounds: int = 3) -> tuple[float, int]:
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        VoiceLexicon.from_entries(entries)
        samples.append((time.perf_counter() - start) * 1000)
    return min(samples), max(samples)


def bench_build(full: list) -> None:
    scales = [1 / 8, 1 / 4, 1 / 2, 1.0]
    for ratio in scales:
        count = max(1, int(len(full) * ratio))
        subset = full[:count]
        low, high = build_time(subset)
        print(
            f"[build] entries={count:>6}  {low:.1f}–{high:.1f} ms "
            f"({low * 1000 / count:.2f} us/条)"
        )


def bench_parse(full: list, label: str = "full") -> None:
    """纯解析耗时：复用单测里的假注册表与状态，不联网。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "voice_parser_cases", ROOT / "backend" / "tests" / "test_voice_parser.py"
    )
    if spec is None or spec.loader is None:
        print("[parse] skipped: cannot load test cases")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from app.fastpath.entities import EntityResolver

    parser = module.VoiceParser(
        VoiceLexicon.from_entries(full), module.ALIASES, module.FakeRegistry()
    )
    resolver = EntityResolver(module.ALIASES, module.FakeRegistry())

    samples: list[float] = []
    for _ in range(100):
        for phrase in PHRASES:
            start = time.perf_counter()
            module.parse(parser, resolver, phrase)
            samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    print(
        "[parse:%s] entries=%d n=%d mean=%.3f p50=%.3f p95=%.3f max=%.3f ms"
        % (
            label,
            len(full),
            len(samples),
            statistics.mean(samples),
            samples[len(samples) // 2],
            samples[int(len(samples) * 0.95)],
            samples[-1],
        )
    )


async def mint_token() -> str:
    async with SessionFactory() as session:
        admin = await session.scalar(
            select(Member).where(Member.role == "admin", Member.enabled.is_(True))
        )
    if admin is None:
        raise SystemExit("no enabled admin member found")
    token, _, _ = create_access_token(admin.id)
    return token


def post_test(text: str, token: str) -> tuple[int, float]:
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/fastpath/voice/test", data=body, method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
            return response.status, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, (time.perf_counter() - started) * 1000


def bench_api() -> None:
    try:
        token = asyncio.run(mint_token())
    except Exception as exc:  # noqa: BLE001
        print(f"[api] skipped: {exc}")
        return

    for phrase in PHRASES:
        status, _ = post_test(phrase, token)
        if status != 200:
            print(f"[api] skipped: HTTP {status} on first probe")
            return

    latencies = []
    for _ in range(20):
        for phrase in PHRASES:
            status, ms = post_test(phrase, token)
            if status == 200:
                latencies.append(ms)
    if not latencies:
        print("[api] no successful samples")
        return
    latencies.sort()
    print(
        "[api] n=%d mean=%.2f p50=%.2f p95=%.2f max=%.2f ms"
        % (
            len(latencies),
            statistics.mean(latencies),
            latencies[len(latencies) // 2],
            latencies[int(len(latencies) * 0.95)],
            latencies[-1],
        )
    )


def main() -> int:
    if not WORKBOOK.exists():
        print(f"workbook missing: {WORKBOOK}")
        return 1

    start = time.perf_counter()
    parsed = parse_workbook(WORKBOOK)
    xlsx_ms = (time.perf_counter() - start) * 1000
    seed = load_seed_entries()
    full = list(parsed.entries) + list(seed)
    print(
        f"[xlsx] rows={len(parsed.entries)} seed={len(seed)} "
        f"total={len(full)} parse={xlsx_ms:.0f} ms"
    )

    bench_build(full)
    bench_parse(full, "full")
    bench_parse(full[: len(full) // 4], "1/4")
    bench_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
