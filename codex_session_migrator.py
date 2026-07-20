#!/usr/bin/env python3
"""兼容脚本入口；具体实现位于 csmigrator 包。"""

from __future__ import annotations

from csmigrator.cli import build_parser, main, run_list, run_migrate_preview, run_restore, scan_from_args
from csmigrator.migration import migrate_sessions
from csmigrator.models import BackupInfo, MigrationResult, ScanIssue, ScanResult, SessionRecord, ThreadRecord, ToolError
from csmigrator.process_guard import ensure_codex_not_running, find_running_codex_processes
from csmigrator.restore import list_backups, load_backup_info, restore_backup
from csmigrator.scanner import (
    default_codex_home as _default_codex_home,
    filter_sessions,
    load_thread_records,
    locate_state_db,
    read_session_meta as _read_session_meta,
    scan_sessions,
)
from csmigrator.selection import (
    choose_backup_interactively,
    choose_sessions_interactively,
    choose_target_provider,
    discover_providers,
    parse_number_selection,
    print_issues,
    print_migration_preview,
    print_restore_preview,
    print_sessions,
    validate_provider,
)

__all__ = [
    "BackupInfo", "MigrationResult", "ScanIssue", "ScanResult", "SessionRecord", "ThreadRecord", "ToolError",
    "build_parser", "choose_backup_interactively", "choose_sessions_interactively", "choose_target_provider",
    "discover_providers", "ensure_codex_not_running", "filter_sessions", "find_running_codex_processes",
    "list_backups", "load_backup_info", "load_thread_records", "locate_state_db", "main", "migrate_sessions",
    "parse_number_selection", "print_issues", "print_migration_preview", "print_restore_preview", "print_sessions",
    "restore_backup", "run_list", "run_migrate_preview", "run_restore", "scan_from_args", "scan_sessions",
    "validate_provider",
]


if __name__ == "__main__":
    raise SystemExit(main())
