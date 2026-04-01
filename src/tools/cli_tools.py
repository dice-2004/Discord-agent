from __future__ import annotations

import os
import shlex
import subprocess


DEFAULT_ALLOWED = [
    "docker ps",
    "docker compose ps",
    "uptime",
    "df -h",
    "free -m",
]


def _allowed_commands() -> list[str]:
    raw = os.getenv("CLI_ALLOWED_COMMANDS", "").strip()
    if not raw:
        return DEFAULT_ALLOWED
    commands = [item.strip() for item in raw.split(",") if item.strip()]
    return commands or DEFAULT_ALLOWED


def _token_valid(approval_token: str | None) -> bool:
    expected = os.getenv("CLI_APPROVAL_TOKEN", "").strip()
    provided = (approval_token or "").strip()
    return bool(expected) and expected == provided


def run_local_cli(command: str, approval_token: str | None = None) -> str:
    """HitL相当: 承認トークンと許可コマンド一致時のみCLIを実行する。"""
    clean_command = (command or "").strip()
    if not clean_command:
        return "実行コマンドが空です。"

    allowed = _allowed_commands()
    if clean_command not in allowed:
        allowed_list = "\n".join(f"- {cmd}" for cmd in allowed)
        return (
            "このコマンドは許可されていません。\n"
            "許可コマンド一覧:\n"
            f"{allowed_list}"
        )

    if not _token_valid(approval_token):
        return "承認トークンが不正です。管理者承認後に再実行してください。"

    try:
        args = shlex.split(clean_command)
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception:
        return "CLI実行に失敗しました。"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    payload = out if out else err if err else "(出力なし)"
    if len(payload) > 3000:
        payload = payload[:3000] + "..."
    return f"[exit={result.returncode}]\n{payload}"
