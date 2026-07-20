"""provider 迁移、备份、回滚与写后校验。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .models import MigrationResult, ScanResult, SessionRecord, ToolError
from .scanner import load_thread_records, read_session_meta
from .selection import validate_provider
from .storage import atomic_write_bytes, copy_sqlite_snapshot, restore_database_snapshot, sha256, updated_session_json


def migration_backups_root(codex_home: Path) -> Path:
    return codex_home / "backups" / "provider-migrations"


def backup_selected_sessions(result: ScanResult, sessions: Sequence[SessionRecord], target_provider: str) -> Path:
    """保存选中 JSONL、数据库一致快照和恢复清单。"""
    backup_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    backup_dir = migration_backups_root(result.codex_home) / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    backups_root = backup_dir / "sessions"
    manifest_sessions: list[dict[str, str]] = []
    for session in sessions:
        try:
            relative_path = session.rollout_path.relative_to(result.codex_home)
        except ValueError as error:
            raise ToolError(f"会话文件不在 Codex 目录内：{session.rollout_path}") from error
        destination = backups_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(session.rollout_path, destination)
        manifest_sessions.append({
            "session_id": session.session_id,
            "rollout_path": relative_path.as_posix(),
            "backup_path": destination.relative_to(backup_dir).as_posix(),
            "from_provider": session.provider,
            "to_provider": target_provider,
            "sha256": sha256(destination),
        })
    database_backup = backup_dir / "state_5.sqlite"
    copy_sqlite_snapshot(result.state_db, database_backup)
    manifest = {
        "format_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "codex_home": str(result.codex_home),
        "state_db": str(result.state_db),
        "database_backup": database_backup.name,
        "database_sha256": sha256(database_backup),
        "target_provider": target_provider,
        "sessions": manifest_sessions,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return backup_dir


def restore_jsonl_from_backup(backup_dir: Path) -> None:
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["sessions"]:
        source = backup_dir / item["backup_path"]
        destination = Path(manifest["codex_home"]) / item["rollout_path"]
        atomic_write_bytes(destination, source.read_bytes())


def verify_migration(state_db: Path, sessions: Sequence[SessionRecord], target_provider: str) -> None:
    threads = load_thread_records(state_db)
    for session in sessions:
        json_provider = read_session_meta(session.rollout_path).get("model_provider")
        db_record = threads.get(session.session_id)
        if json_provider != target_provider or db_record is None or db_record.model_provider != target_provider:
            raise ToolError(f"写入后验证失败：{session.session_id}")


def migrate_sessions(result: ScanResult, sessions: Sequence[SessionRecord], target_provider: str) -> MigrationResult:
    """备份并同步更新选中 JSONL 和 SQLite；失败时回滚到备份。"""
    if not sessions:
        raise ToolError("没有可迁移的会话")
    target_provider = validate_provider(target_provider)
    invalid = [session.session_id for session in sessions if not session.selectable]
    if invalid:
        raise ToolError(f"异常会话不能迁移：{', '.join(invalid)}")
    updates = {session.rollout_path: updated_session_json(session.rollout_path, session.provider, target_provider) for session in sessions}
    backup_dir = backup_selected_sessions(result, sessions, target_provider)
    replaced_jsonl = False
    connection = sqlite3.connect(result.state_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for session in sessions:
            cursor = connection.execute(
                "UPDATE threads SET model_provider = ? WHERE id = ? AND model_provider = ?",
                (target_provider, session.session_id, session.provider),
            )
            if cursor.rowcount != 1:
                raise ToolError(f"数据库 provider 已变化，拒绝覆盖：{session.session_id}")
        for path, content in updates.items():
            atomic_write_bytes(path, content)
            replaced_jsonl = True
        connection.commit()
        verify_migration(result.state_db, sessions, target_provider)
    except Exception as error:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        restore_errors: list[str] = []
        if replaced_jsonl:
            try:
                restore_jsonl_from_backup(backup_dir)
            except (OSError, ToolError, json.JSONDecodeError) as restore_error:
                restore_errors.append(f"JSONL 恢复失败：{restore_error}")
        try:
            restore_database_snapshot(backup_dir / "state_5.sqlite", result.state_db)
        except (OSError, sqlite3.Error, ToolError) as restore_error:
            restore_errors.append(f"SQLite 恢复失败：{restore_error}")
        if restore_errors:
            raise ToolError(f"迁移失败，自动回退不完整，请使用备份目录手工恢复：{backup_dir}（{'；'.join(restore_errors)}）") from error
        if isinstance(error, ToolError):
            raise
        raise ToolError(f"迁移失败，已从备份回退：{error}") from error
    finally:
        connection.close()
    return MigrationResult(backup_dir, tuple(session.session_id for session in sessions), target_provider)
