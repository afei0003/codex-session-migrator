"""交互式会话与 provider 选择，以及终端预览输出。"""

from __future__ import annotations

import tomllib
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from .models import BackupInfo, ScanIssue, ScanResult, SessionRecord, ToolError


def parse_number_selection(value: str, maximum: int) -> list[int]:
    """解析 1,3-5 格式，拒绝空选、全选捷径和越界编号。"""
    if maximum < 1:
        raise ToolError("没有可供选择的项目")
    if not value.strip():
        raise ToolError("未选择任何项目")
    indexes: list[int] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            raise ToolError("选择格式包含空项")
        bounds = token.split("-", maxsplit=1)
        try:
            start, end = int(bounds[0]), int(bounds[-1])
        except ValueError as error:
            raise ToolError("请选择编号，例如 1,3-5") from error
        if start < 1 or end < 1 or start > end or end > maximum:
            raise ToolError(f"编号必须在 1 到 {maximum} 之间")
        indexes.extend(range(start, end + 1))
    return list(dict.fromkeys(indexes))


def short_title(value: str, limit: int = 54) -> str:
    flattened = " ".join(value.split())
    return flattened if len(flattened) <= limit else f"{flattened[: limit - 1]}…"


def print_sessions(sessions: Sequence[SessionRecord]) -> None:
    if not sessions:
        print("没有符合条件的会话。")
        return
    for index, session in enumerate(sessions, start=1):
        archived = " [已归档]" if session.archived else ""
        print(f"[{index:>3}] {session.session_date.isoformat()} | {session.session_id} | {session.provider} | {session.status}{archived} | {short_title(session.title)}")


def print_issues(issues: Sequence[ScanIssue]) -> None:
    if not issues:
        return
    print(f"\n发现 {len(issues)} 项异常（只报告，不进入迁移选择）：")
    for issue in issues:
        location = f" | {issue.path}" if issue.path else ""
        print(f"- {issue.code}：{issue.message}{location}")


def choose_sessions_interactively(sessions: Sequence[SessionRecord], input_fn: Callable[[str], str] = input) -> list[SessionRecord]:
    """先选日期，再选对应会话；始终拒绝一键全选。"""
    selectable = [session for session in sessions if session.selectable]
    if not selectable:
        raise ToolError("没有 JSONL 与数据库一致的可选会话")
    groups: dict[date, list[SessionRecord]] = defaultdict(list)
    for session in selectable:
        groups[session.session_date].append(session)
    dates = sorted(groups, reverse=True)
    print("可选择日期：")
    for index, session_date in enumerate(dates, start=1):
        print(f"[{index}] {session_date.isoformat()}（{len(groups[session_date])} 条）")
    date_indexes = parse_number_selection(input_fn("选择日期编号（例如 1,3-5）："), len(dates))
    chosen_dates = {dates[index - 1] for index in date_indexes}
    candidates = [session for session in selectable if session.session_date in chosen_dates]
    print("\n可迁移会话：")
    print_sessions(candidates)
    indexes = parse_number_selection(input_fn("选择会话编号（例如 1,3-5）："), len(candidates))
    return [candidates[index - 1] for index in indexes]


def _configured_providers(codex_home: Path) -> set[str]:
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        return set()
    try:
        with config_path.open("rb") as file:
            config = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    providers: set[str] = set()
    current = config.get("model_provider")
    if isinstance(current, str) and current.strip():
        providers.add(current.strip())
    configured = config.get("model_providers")
    if isinstance(configured, dict):
        providers.update(name.strip() for name in configured if isinstance(name, str) and name.strip())
    return providers


def discover_providers(result: ScanResult) -> list[str]:
    providers = _configured_providers(result.codex_home)
    for session in result.sessions:
        if session.json_provider:
            providers.add(session.json_provider)
        if session.database_provider:
            providers.add(session.database_provider)
    return sorted(providers, key=lambda value: (value.casefold(), value))


def validate_provider(value: str) -> str:
    provider = value.strip()
    if not provider:
        raise ToolError("目标 provider 不能为空")
    if any(character in provider for character in "\r\n\x00"):
        raise ToolError("目标 provider 不能包含换行符或空字符")
    return provider


def choose_target_provider(providers: Sequence[str], input_fn: Callable[[str], str] = input) -> str:
    print("可用目标 provider：")
    for index, provider in enumerate(providers, start=1):
        print(f"[{index}] {provider}")
    print("[m] 手工输入新的 provider")
    choice = input_fn("选择目标 provider 编号，或输入 m：").strip()
    if choice.casefold() == "m":
        return validate_provider(input_fn("输入目标 provider（区分大小写）："))
    try:
        index = int(choice)
    except ValueError as error:
        raise ToolError("请输入 provider 编号，或输入 m") from error
    if index < 1 or index > len(providers):
        raise ToolError(f"provider 编号必须在 1 到 {len(providers)} 之间")
    return providers[index - 1]


def print_migration_preview(sessions: Sequence[SessionRecord], target_provider: str) -> None:
    print("\n迁移预览：")
    for index, session in enumerate(sessions, start=1):
        print(f"[{index:>3}] {session.session_id} | {short_title(session.title)} | {session.provider} -> {target_provider}")


def choose_backup_interactively(backups: Sequence[BackupInfo], input_fn: Callable[[str], str] = input) -> BackupInfo:
    if not backups:
        raise ToolError("没有找到可恢复的有效备份")
    print("可恢复备份：")
    for index, backup in enumerate(backups, start=1):
        manifest = backup.manifest
        print(f"[{index}] {backup.backup_dir.name} | {manifest.get('created_at', '未知时间')} | {len(manifest.get('sessions', []))} 条 | {manifest.get('target_provider', '未知 provider')}")
    indexes = parse_number_selection(input_fn("选择一个备份编号："), len(backups))
    if len(indexes) != 1:
        raise ToolError("一次只能恢复一个备份")
    return backups[indexes[0] - 1]


def print_restore_preview(backup: BackupInfo) -> None:
    manifest = backup.manifest
    print("恢复预览：")
    print(f"- 备份：{backup.backup_dir.name}")
    print(f"- 创建时间：{manifest.get('created_at', '未知时间')}")
    print(f"- 会话数量：{len(manifest.get('sessions', []))}")
    print(f"- 备份目标 provider：{manifest.get('target_provider', '未知 provider')}")
    for item in manifest.get("sessions", []):
        if isinstance(item, dict):
            print(f"  - {item.get('session_id', '未知 Session ID')} | 恢复为 {item.get('from_provider', '未知 provider')}")
