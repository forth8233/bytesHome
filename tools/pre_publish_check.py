"""发布前密钥自检：扫描明文密钥，并确认敏感文件确实被 .gitignore 覆盖。

只读、不联网、不打印完整密钥；密钥一律打码显示。

用法（任意目录均可）：
    backend/.venv/Scripts/python.exe tools/pre_publish_check.py

退出码：0 无风险，1 发现会随 git 提交的密钥或未被忽略的敏感文件。
注意：本脚本只能防 git 推送。网页拖拽上传或打包 zip 上传会绕过 .gitignore。
"""

from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    ".vite",
    "data",
    "logs",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
}

PATTERNS = [
    ("API 密钥", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("HA 长期令牌", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "疑似密钥赋值",
        re.compile(
            r"(?i)(api[_-]?key|secret|password|encryption[_-]?key)\s*[:=]\s*"
            r"[\"']([A-Za-z0-9_\-+/=]{20,})[\"']"
        ),
    ),
]

SENSITIVE = [
    ".env",
    "backend/.env",
    "backend/data/agent.db",
    "data/agent.db",
    "logs/uvicorn.log",
    ".claude/settings.local.json",
]

MAX_BYTES = 2_000_000
_TMP_REPO: Path | None = None


def mask(secret: str) -> str:
    head = secret[:3] if secret[:3].isprintable() else ""
    return f"{head}***（共 {len(secret)} 位）"


def is_git_repo() -> bool:
    return (ROOT / ".git").exists()


def _ignore_repo() -> Path:
    """真实仓库直接用；否则建临时仓库复刻 .gitignore，行为与 git 一致。"""
    global _TMP_REPO
    if is_git_repo():
        return ROOT
    if _TMP_REPO is None:
        tmp = Path(tempfile.mkdtemp(prefix="ignorecheck-"))
        shutil.copy(ROOT / ".gitignore", tmp / ".gitignore")
        subprocess.run(["git", "init", "-q"], cwd=tmp, capture_output=True)
        atexit.register(shutil.rmtree, tmp, True)
        _TMP_REPO = tmp
    return _TMP_REPO


def is_ignored(rel_path: str) -> bool:
    repo = _ignore_repo()
    return (
        subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", rel_path],
            capture_output=True,
        ).returncode
        == 0
    )


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        yield path


def scan_secrets() -> list[tuple[str, int, str, str]]:
    findings: list[tuple[str, int, str, str]] = []
    for path in iter_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    findings.append((rel, lineno, label, mask(match.group(0))))
    return findings


def tracked_sensitive() -> list[str]:
    if not is_git_repo():
        return []
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True
    ).stdout
    tracked = set(output.splitlines())
    return [path for path in SENSITIVE if path in tracked]


def main() -> int:
    print(f"仓库根目录：{ROOT}")
    print("=" * 60)

    findings = scan_secrets()
    blocking = [item for item in findings if not is_ignored(item[0])]
    advisory = [item for item in findings if is_ignored(item[0])]

    if blocking:
        print(f"[阻断] 会随 git 提交的明文密钥 {len(blocking)} 处：")
        for rel, lineno, label, shown in blocking:
            print(f"  - {rel}:{lineno}  {label}  {shown}")
    else:
        print("[通过] 待提交文件中未发现明文密钥")

    if advisory:
        print(f"[提示] 已忽略文件中的明文密钥 {len(advisory)} 处（不会进 git，勿打包上传）：")
        for rel, lineno, label, shown in advisory:
            print(f"  - {rel}:{lineno}  {label}  {shown}")

    print("-" * 60)
    unsafe = [path for path in SENSITIVE if not is_ignored(path)]
    for path in SENSITIVE:
        print(f"  {'[已忽略]' if path not in unsafe else '[未忽略]'} {path}")

    print("-" * 60)
    tracked = tracked_sensitive()
    if tracked:
        print(f"[失败] 以下敏感文件已被 git 跟踪：{', '.join(tracked)}")
        print("       需要 git rm --cached 并从提交历史中清理")
    elif is_git_repo():
        print("[通过] 敏感文件均未被 git 跟踪")
    else:
        print("[提示] 当前目录还不是 git 仓库，已按 .gitignore 规则做等价校验")

    print("=" * 60)
    if blocking or unsafe or tracked:
        print("结论：存在风险，先修复再发布。")
        print("提醒：网页上传或打包 zip 上传会绕过 .gitignore，务必用 git push。")
        return 1
    print("结论：自检通过。提醒：网页上传或 zip 上传会绕过 .gitignore。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
