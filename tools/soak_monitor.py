#!/usr/bin/env python3
"""M6 soak monitor: poll liveness/readiness and a read-only fast-path probe.

Usage:
    backend/.venv/Scripts/python.exe tools/soak_monitor.py --hours 72

Credentials are read from SOAK_USERNAME / SOAK_PASSWORD. Passing --password on the
command line exposes it in the process list, so prefer the environment variables.

Every sample is appended to logs/soak.jsonl. A run exits non-zero after
--max-consecutive-failures consecutive liveness failures so a supervisor can alert.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent


def http_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict, float]:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            return (
                response.status,
                json.loads(body or b"{}"),
                (time.perf_counter() - started) * 1000,
            )
    except error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body or b"{}")
        except ValueError:
            parsed = {"error": body[:300].decode("utf-8", "replace")}
        return exc.code, parsed, (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001 - monitor must keep sampling
        return 0, {"error": str(exc)}, (time.perf_counter() - started) * 1000


def login(base_url: str, username: str, password: str) -> str | None:
    status, body, _ = http_json(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
    )
    if status != 200:
        print(f"login failed: HTTP {status} {body.get('message') or body.get('error')}")
        return None
    return body.get("access_token")


def sample(base_url: str, token: str | None, probe_text: str) -> dict:
    health_status, health_body, _ = http_json(base_url, "/healthz", timeout=5)
    ready_status, ready_body, _ = http_json(base_url, "/readyz", timeout=5)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "healthz_status": health_status,
        "healthz_ok": health_status == 200 and health_body.get("status") == "ok",
    }
    row["readyz_status"] = ready_status
    row["readyz_state"] = (
        "ready"
        if ready_status == 200
        else "degraded"
        if ready_status == 503
        else "error"
    )
    checks = ready_body.get("checks") if isinstance(ready_body, dict) else None
    if isinstance(checks, dict):
        row["checks"] = checks

    if token:
        status, body, latency_ms = http_json(
            base_url,
            "/api/fastpath/test",
            method="POST",
            payload={"text": probe_text},
            token=token,
            timeout=10,
        )
        row["fastpath_status"] = status
        row["fastpath_ok"] = status == 200
        row["fastpath_latency_ms"] = round(latency_ms, 2)
        if isinstance(body, dict):
            row["fastpath_route"] = body.get("route")
    return row


def summarize(rows: list[dict]) -> str:
    if not rows:
        return "no samples"
    total = len(rows)
    health = sum(1 for row in rows if row.get("healthz_ok"))
    ready = sum(1 for row in rows if row.get("readyz_state") == "ready")
    degraded = sum(1 for row in rows if row.get("readyz_state") == "degraded")
    latencies = [
        row["fastpath_latency_ms"] for row in rows if "fastpath_latency_ms" in row
    ]
    lines = [
        f"samples={total}",
        f"healthz_uptime={health / total:.2%}",
        f"ready={ready / total:.2%}",
        f"degraded={degraded / total:.2%}",
    ]
    if latencies:
        ordered = sorted(latencies)

        def pct(value: float) -> float:
            index = min(len(ordered) - 1, max(0, int(round(value * (len(ordered) - 1)))))
            return round(ordered[index], 2)

        lines.append(
            "fastpath_ms="
            f"p50:{pct(0.50)} p95:{pct(0.95)} p99:{pct(0.99)} max:{round(ordered[-1], 2)}"
        )
    return " ".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Home Agent soak monitor")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--hours", type=float, default=72.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--probe-text", default="客厅温度")
    parser.add_argument("--username", default=os.environ.get("SOAK_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("SOAK_PASSWORD", ""))
    parser.add_argument("--output", default=str(ROOT / "logs" / "soak.jsonl"))
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    args = parser.parse_args()

    token = None
    next_login = 0.0
    if args.username and args.password:
        token = login(args.base_url, args.username, args.password)
        if token is None:
            print("continuing without authenticated fast-path probe")
        else:
            next_login = time.monotonic() + 3600

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.hours * 3600
    rows: list[dict] = []
    consecutive_failures = 0

    with output.open("a", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            if args.username and args.password:
                now = time.monotonic()
                if token is None or now >= next_login:
                    refreshed = login(args.base_url, args.username, args.password)
                    if refreshed:
                        token = refreshed
                        next_login = now + 3600
                        print("access token refreshed")
                    else:
                        next_login = now + 60
            row = sample(args.base_url, token, args.probe_text)
            rows.append(row)
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()

            consecutive_failures = consecutive_failures + 1 if not row["healthz_ok"] else 0
            ready_state = row["readyz_state"]
            print(
                f"{row['ts']} liveness={'ok' if row['healthz_ok'] else 'FAIL'} "
                f"ready={ready_state}"
                + (
                    f" fastpath={row.get('fastpath_status')} "
                    f"{row.get('fastpath_latency_ms')}ms"
                    if "fastpath_latency_ms" in row
                    else ""
                )
            )
            if consecutive_failures >= args.max_consecutive_failures:
                print(
                    f"ABORT: {consecutive_failures} consecutive liveness failures",
                    file=sys.stderr,
                )
                print(summarize(rows))
                return 2
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))

    print(summarize(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
