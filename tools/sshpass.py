#!/usr/bin/env python3
"""轻量 sshpass 封装：用 pty 自动应答密码，执行远端命令并打印输出。
用法: python tools/sshpass.py <host> <user> <password> <remote_command_string>
"""
import os
import pty
import select
import subprocess
import sys

HOST, USER, PASSWORD, REMOTE = sys.argv[1:5]

def main():
    pid, fd = pty.fork()
    if pid == 0:  # 子进程
        os.execvp("ssh", [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "PreferredAuthentications=password",
            "-o", "PubkeyAuthentication=no",
            f"{USER}@{HOST}", REMOTE,
        ])

    output = []
    sent_pw = False
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 5)
        except OSError:
            break
        if fd not in r:
            break
        try:
            chunk = os.read(fd, 4096).decode("utf-8", "replace")
        except OSError:
            break
        if not chunk:
            break
        output.append(chunk)
        if not sent_pw and ("password" in chunk.lower() or "Password" in chunk):
            os.write(fd, (PASSWORD + "\n").encode())
            sent_pw = True
        elif "Permission denied" in chunk:
            # 密码错误，继续吃输出但不重发
            pass
    try:
        os.close(fd)
    except OSError:
        pass
    # 清理控制字符，仅保留可读输出
    text = "".join(output)
    clean = "".join(c for c in text if c.isprintable() or c == "\n")
    print(clean)
    return 0 if not ("Permission denied" in text and "last login" not in text) else 1

if __name__ == "__main__":
    sys.exit(main())