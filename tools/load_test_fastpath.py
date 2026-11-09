#!/usr/bin/env python3
"""M6 read-only fast-path load probe.

Hits POST /api/fastpath/test, which parses and resolves without calling Home
Assistant. Never point this at /api/chat: that would execute device actions.

Usage:
    SOAK_USERNAME=admin SOAK_PASSWORD=*** \
        backend/.venv/Scripts/python.exe tools/load_test_fastpath.py --requests 200 --concurrency 4
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import error, request

PROBES = [
    "客厅温度",
    "现在家里温度",
    "卧室湿度",
    "书房灯开着吗",
    "空调现在多少度",
]


def post(url: str, payload: dict, token: str, timeout: float) -> tuple[int, float, str]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            response.read()
            return response.status, (time.perf_counter() - started) * 1000, ""
    except error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        return exc.code, (time.perf_counter() - started) * 1000, detail
    except Exception as exc:  # noqa: BLE001 - report transport failures
        return 0, (time.perf_counter() - started) * 1000, str(exc)


def login(base_url: str, username: str, password: str, timeout: float) -> str | None:
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read()).get("access_token")
    except error.HTTPError as exc:
        print(f"login failed: HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"login failed: {exc}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast-path read-only load probe")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--username", default=os.environ.get("SOAK_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("SOAK_PASSWORD", ""))
    args = parser.parse_args()

    if not args.username or not args.password:
        print("set SOAK_USERNAME / SOAK_PASSWORD (or pass --username/--password)")
        return 1

    token = login(args.base_url, args.username, args.password, args.timeout)
    if not token:
        return 1

    url = f"{args.base_url.rstrip('/')}/api/fastpath/test"
    tasks = [PROBES[index % len(PROBES)] for index in range(args.requests)]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(
            pool.map(
                lambda text: post(url, {"text": text}, token, args.timeout),
                tasks,
            )
        )
    wall_ms = (time.perf_counter() - started) * 1000

    latencies = sorted(result[1] for result in results)
    failures = [result for result in results if result[0] != 200]
    non_200 = {
        status: sum(1 for item in results if item[0] == status)
        for status in {item[0] for item in results}
    }

    def pct(value: float) -> float:
        index = min(len(latencies) - 1, max(0, int(round(value * (len(latencies) - 1)))))
        return round(latencies[index], 2)

    print(
        f"requests={args.requests} concurrency={args.concurrency} "
        f"wall={wall_ms:.0f}ms rps={args.requests / (wall_ms / 1000):.1f}"
    )
    print(
        f"latency_ms p50={pct(0.50)} p95={pct(0.95)} p99={pct(0.99)} "
        f"max={round(latencies[-1], 2)}"
    )
    print(f"non_200={sum(1 for item in results if item[0] != 200)} statuses={non_200}")
    if failures:
        for status, latency_ms, detail in failures[:5]:
            print(f"failure HTTP {status} {latency_ms:.1f}ms {detail}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
