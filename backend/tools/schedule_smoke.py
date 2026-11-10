# -*- coding: utf-8 -*-
"""schedule.parse_schedule 冒烟测试（临时）。"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.fastpath.schedule import parse_schedule

CASES = [
    "5分钟后开灯",
    "再过20分钟关空调",
    "凌晨开灯",
    "11点开灯",
    "11点20分开灯",
    "晚上11点开窗帘",
    "下午3点开空调",
    "筒灯开5分钟",
    "空调开2小时",
    "开灯",
    "太亮了",
    "并打开灯",
    "后台5分钟",
    "每天8点开灯",
]

def fmt(run_at):
    return run_at.strftime("%m-%d %H:%M") if run_at else "-"

for c in CASES:
    r = parse_schedule(c)
    if r is None:
        print(f"{c!r:24} -> None")
    else:
        print(f"{c!r:24} -> kind={r.kind:8} summary={r.summary!r:16} run_at={fmt(r.run_at)} action={r.action_text!r} auto_at={fmt(r.auto_run_at)}")