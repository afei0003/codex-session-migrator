"""命令行参数、交互编排与进程退出码。"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from .migration import migrate_sessions
from .models import ScanResult, SessionRecord, ToolError
from .process_guard import ensure_codex_not_running
from .restore import list_backups, load_backup_info, restore_backup
from .scanner import default_codex_home, filter_sessions, locate_state_db, scan_sessions
from .selection import (
    choose_backup_interactively,
    choose_sessions_interactively,
    choose_target_provider,
    discover_providers,
    print_issues,
    print_migration_preview,
    print_restore_preview,
    print_sessions,
    validate_provider,
)


PROGRAM_NAME = "codex-session-migrator"
VERSION = "0.2.0"


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式") from error


def add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", type=Path, help="Codex 数据目录，默认 ~/.codex")
    parser.add_argument("--state-db", type=Path, help="显式指定 state_5.sqlite")
    parser.add_argument("--from", dest="date_from", type=parse_date, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=parse_date, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--provider", help="按当前 provider 精确筛选（区分大小写）")
    parser.add_argument("--session-id", nargs="+", help="按一个或多个 Session ID 筛选")
    parser.add_argument("--include-archived", action="store_true", help="包含已归档会话")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM_NAME, description="安全扫描、选择并迁移 Codex 会话的 model_provider。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    list_parser = subparsers.add_parser("list", help="扫描并列出 Codex 会话")
    add_scan_arguments(list_parser)
    migrate_parser = subparsers.add_parser("migrate", help="预览并迁移会话 provider")
    add_scan_arguments(migrate_parser)
    migrate_parser.add_argument("--to-provider", help="目标 provider（区分大小写）")
    migrate_parser.add_argument("--dry-run", action="store_true", help="仅预览，不检查进程且不写入")
    restore_parser = subparsers.add_parser("restore", help="从迁移备份恢复会话")
    restore_parser.add_argument("--codex-home", type=Path, help="Codex 数据目录，默认 ~/.codex")
    restore_parser.add_argument("--state-db", type=Path, help="显式指定 state_5.sqlite")
    restore_parser.add_argument("--backup", type=Path, help="备份目录")
    restore_parser.add_argument("--dry-run", action="store_true", help="仅校验并预览，不检查进程且不写入")
    return parser


def scan_from_args(args: argparse.Namespace) -> tuple[ScanResult, list[SessionRecord]]:
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise ToolError("--from 不能晚于 --to")
    result = scan_sessions(args.codex_home, args.state_db, args.include_archived)
    filtered = filter_sessions(result.sessions, args.date_from, args.date_to, args.provider, args.session_id, args.include_archived)
    if args.session_id:
        found_ids = {session.session_id for session in result.sessions}
        missing = [session_id for session_id in args.session_id if session_id not in found_ids]
        if missing:
            raise ToolError(f"未找到 Session ID：{', '.join(missing)}")
    return result, filtered


def run_list(args: argparse.Namespace) -> int:
    result, filtered = scan_from_args(args)
    print(f"Codex 目录：{result.codex_home}")
    print(f"活动数据库：{result.state_db}")
    print(f"扫描到 {len(result.sessions)} 条会话，筛选后 {len(filtered)} 条。\n")
    print_sessions(filtered)
    print_issues(result.issues)
    return 0


def run_migrate_preview(args: argparse.Namespace) -> int:
    result, filtered = scan_from_args(args)
    if args.session_id:
        selected = [session for session in filtered if session.selectable]
        unselectable = [session for session in filtered if not session.selectable]
        if unselectable:
            raise ToolError(f"以下会话状态异常，不能选择：{', '.join(session.session_id for session in unselectable)}")
        if not selected:
            raise ToolError("没有可选择的会话")
    else:
        print(f"Codex 目录：{result.codex_home}")
        print(f"活动数据库：{result.state_db}\n")
        selected = choose_sessions_interactively(filtered)
    target_provider = validate_provider(args.to_provider) if args.to_provider else choose_target_provider(discover_providers(result))
    changes = [session for session in selected if session.provider != target_provider]
    if not changes:
        print("所有选中会话已属于目标 provider，未执行任何写入。")
        return 0
    print_migration_preview(changes, target_provider)
    print_issues(result.issues)
    if args.dry_run:
        print("\n--dry-run：仅完成预览，未检查进程且未写入数据。")
        return 0
    ensure_codex_not_running()
    expected_confirmation = f"MIGRATE {len(changes)}"
    confirmation = input(f"输入 {expected_confirmation} 以创建备份并执行迁移：")
    if confirmation.strip() != expected_confirmation:
        print("确认短语不匹配，已取消，未写入任何数据。")
        return 0
    migration = migrate_sessions(result, changes, target_provider)
    print("\n迁移完成，JSONL 与 SQLite 已通过写后校验。")
    print(f"备份目录：{migration.backup_dir}")
    return 0


def run_restore(args: argparse.Namespace) -> int:
    codex_home = (args.codex_home or default_codex_home()).expanduser().resolve()
    if not codex_home.is_dir():
        raise ToolError(f"找不到 Codex 数据目录：{codex_home}")
    state_db = locate_state_db(codex_home, args.state_db)
    backup = load_backup_info(args.backup, codex_home) if args.backup else choose_backup_interactively(list_backups(codex_home))
    print_restore_preview(backup)
    if args.dry_run:
        print("\n--dry-run：仅完成备份校验与预览，未检查进程且未写入数据。")
        return 0
    ensure_codex_not_running()
    expected_confirmation = f"RESTORE {backup.backup_dir.name}"
    confirmation = input(f"输入 {expected_confirmation} 以备份当前状态并恢复：")
    if confirmation.strip() != expected_confirmation:
        print("确认短语不匹配，已取消，未写入任何数据。")
        return 0
    rollback_backup = restore_backup(backup, codex_home, state_db)
    print("\n恢复完成，JSONL 与 SQLite 已通过校验。")
    print(f"恢复前状态备份：{rollback_backup}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv) or ["migrate"]
    args = parser.parse_args(raw_args)
    try:
        if args.command == "list":
            return run_list(args)
        if args.command == "migrate":
            return run_migrate_preview(args)
        if args.command == "restore":
            return run_restore(args)
        parser.print_help()
        return 0
    except (ToolError, OSError, sqlite3.Error) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return 130
