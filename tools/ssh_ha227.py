#!/usr/bin/env python3
"""SSH helper that runs a remote command with password auth (paramiko).

Usage:
    python ssh_ha227.py "<remote command>"

Connects to 192.168.2.127 as tantron, passing the password via an env var
HA227_PW (so it never appears on a command line). Requires paramiko.
"""
import os
import sys

import paramiko

HOST = "192.168.2.127"
USER = "tantron"
PORT = 22


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    pw = os.environ.get("HA227_PW", "")
    if not pw:
        print("ERROR: set HA227_PW env var", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            HOST,
            port=PORT,
            username=USER,
            password=pw,
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if out:
            print(out, end="")
        if err and rc != 0:
            print("[stderr]", file=sys.stderr)
            print(err, end="", file=sys.stderr)
        return rc
    except paramiko.AuthenticationException:
        print("ERROR: authentication failed", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())