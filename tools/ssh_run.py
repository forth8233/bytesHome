#!/usr/bin/env python3
"""通过 paramiko SSH 远程执行命令。
用法: python tools/ssh_run.py <host> <user> <password> <command>
"""
import sys
import paramiko

host, user, password, command = sys.argv[1:5]
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(host, username=user, password=password, timeout=20, banner_timeout=20, auth_timeout=20)
except Exception as e:
    print(f"CONNECT_ERROR: {e}", file=sys.stderr)
    sys.exit(2)

stdin, stdout, stderr = client.exec_command(command, timeout=120)
out = stdout.read().decode("utf-8", "replace")
err = stderr.read().decode("utf-8", "replace")
if out:
    print(out)
if err:
    print("---STDERR---")
    print(err)
client.close()