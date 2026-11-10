#!/usr/bin/env python3
"""SFTP + rebuild deploy helper for 227.

Uploads backend source files by SFTP (atomic: write dst.new then mv), then
runs the docker compose rebuild + restart remotely. Mirrors the manual steps in
project_ha227 deploy memory; uses HA227_PW env var for password auth.

Usage:
    python sftp_deploy_227.py backend/app/channels/feishu.py backend/app/channels/manager.py
"""
from __future__ import annotations

import os
import sys
import posixpath

import paramiko

HOST = "192.168.2.227"
USER = "tantron"
PORT = 22
REMOTE_BASE = "/home/tantron/bytesHome"


def main() -> int:
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not files:
        print(__doc__)
        return 2
    pw = os.environ.get("HA227_PW", "")
    if not pw:
        print("ERROR: set HA227_PW env var", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST, port=PORT, username=USER, password=pw, timeout=15,
        allow_agent=False, look_for_keys=False,
    )
    sftp = client.open_sftp()
    try:
        # 1. upload all files atomically
        for local in files:
            norm = local.replace("\\", "/")
            dst = posixpath.join(REMOTE_BASE, norm)
            print(f"upload {local} -> {dst}")
            sftp.put(local, dst + ".new")
            sftp.posix_rename(dst + ".new", dst)
        # 2. rebuild + restart agent, wait for healthy
        cmds = "\n".join([
            "cd /home/tantron/bytesHome",
            "docker compose build agent 2>&1 | tail -5",
            "docker compose up -d agent 2>&1 | tail -5",
            "for i in $(seq 1 40); do "
            "  s=$(docker inspect --format '{{.State.Health.Status}}' home-agent 2>/dev/null); "
            "  echo \"health=$s\"; [ \"$s\" = \"healthy\" ] && break; sleep 3; done",
        ])
        print("--- remote build/restart ---")
        stdin, stdout, stderr = client.exec_command(cmds, timeout=600)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        if out:
            print(out, end="")
        if err:
            print("[stderr]\n" + err, end="")
        print("remote RC =", rc)
        return rc
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    sys.exit(main())