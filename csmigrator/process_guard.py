"""写入前的 Codex 进程保护。"""

from __future__ import annotations

import csv
import os
import subprocess
from pathlib import Path

from .models import ToolError


def find_running_codex_processes() -> list[str]:
    """返回可能写入 Codex 数据的客户端进程名称。"""
    candidates = {"codex", "codex.exe", "codex-desktop", "codex-desktop.exe"}
    try:
        if os.name == "nt":
            completed = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if completed.returncode == 0:
                names = [row[0] for row in csv.reader(completed.stdout.splitlines()) if row]
            else:
                fallback = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Process | Select-Object -ExpandProperty ProcessName"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if fallback.returncode != 0:
                    raise ToolError("无法检测 Codex 进程，拒绝写入以保护会话数据")
                names, completed = [line.strip() for line in fallback.stdout.splitlines() if line.strip()], fallback
        else:
            completed = subprocess.run(["ps", "-A", "-o", "comm="], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
            names = [Path(line.strip()).name for line in completed.stdout.splitlines() if line.strip()]
    except OSError as error:
        raise ToolError(f"无法检测 Codex 进程：{error}") from error
    if completed.returncode != 0:
        raise ToolError("无法检测 Codex 进程，拒绝写入以保护会话数据")
    return list(dict.fromkeys(name for name in names if name.casefold() in candidates))


def ensure_codex_not_running() -> None:
    processes = find_running_codex_processes()
    if processes:
        raise ToolError(f"检测到 Codex 正在运行（{', '.join(processes)}），请彻底关闭后再迁移")
