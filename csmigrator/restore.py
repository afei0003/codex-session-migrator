"""迁移备份的校验、选择、恢复和恢复后回滚。"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .migration import migration_backups_root
from .models import BackupInfo, ToolError
from .scanner import load_thread_records, read_session_meta
from .storage import atomic_write_bytes, copy_sqlite_snapshot, restore_database_snapshot, sha256


def safe_relative_path(value: object, description: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ToolError(f"备份清单缺少 {description}")
    path = Path(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ToolError(f"备份清单中的 {description} 不是安全相对路径：{value}")
    return path


def manifest_sessions(manifest: dict[str, object]) -> list[dict[str, object]]:
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ToolError("备份清单不包含会话文件")
    if not all(isinstance(session, dict) for session in sessions):
        raise ToolError("备份清单包含无效会话项")
    return sessions


def load_backup_info(backup_dir: Path, codex_home: Path) -> BackupInfo:
    """加载并验证清单、哈希和恢复目标，拒绝跨目录恢复。"""
    resolved_dir = backup_dir.expanduser().resolve()
    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ToolError(f"备份目录不包含 manifest.json：{resolved_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ToolError(f"无法读取备份清单：{manifest_path}") from error
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ToolError(f"不支持的备份清单格式：{manifest_path}")
    manifest_home = manifest.get("codex_home")
    if not isinstance(manifest_home, str):
        raise ToolError("备份清单缺少 codex_home")
    if Path(manifest_home).expanduser().resolve() != codex_home.resolve():
        raise ToolError("备份属于其他 Codex 数据目录，拒绝恢复")
    database_path = resolved_dir / safe_relative_path(manifest.get("database_backup"), "database_backup")
    expected_database_hash = manifest.get("database_sha256")
    if not database_path.is_file() or not isinstance(expected_database_hash, str) or sha256(database_path) != expected_database_hash:
        raise ToolError(f"备份数据库哈希校验失败：{database_path}")
    for session in manifest_sessions(manifest):
        source = resolved_dir / safe_relative_path(session.get("backup_path"), "backup_path")
        destination = codex_home / safe_relative_path(session.get("rollout_path"), "rollout_path")
        expected_hash = session.get("sha256")
        if not source.is_file() or not isinstance(expected_hash, str) or sha256(source) != expected_hash:
            raise ToolError(f"会话备份哈希校验失败：{source}")
        if not destination.is_file():
            raise ToolError(f"当前会话文件不存在，拒绝覆盖：{destination}")
    return BackupInfo(resolved_dir, manifest)


def list_backups(codex_home: Path) -> list[BackupInfo]:
    root = migration_backups_root(codex_home)
    if not root.is_dir():
        return []
    backups: list[BackupInfo] = []
    for directory in sorted((path for path in root.iterdir() if path.is_dir()), reverse=True):
        try:
            backups.append(load_backup_info(directory, codex_home))
        except ToolError:
            continue
    return backups


def backup_current_state_for_restore(codex_home: Path, state_db: Path, source_backup: BackupInfo) -> BackupInfo:
    backup_id = f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')}-before-restore"
    backup_dir = migration_backups_root(codex_home) / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    sessions_root = backup_dir / "sessions"
    manifest_items: list[dict[str, str]] = []
    for item in manifest_sessions(source_backup.manifest):
        relative = safe_relative_path(item.get("rollout_path"), "rollout_path")
        source, destination = codex_home / relative, sessions_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest_items.append({
            "session_id": str(item.get("session_id") or ""),
            "rollout_path": relative.as_posix(),
            "backup_path": destination.relative_to(backup_dir).as_posix(),
            "from_provider": str(read_session_meta(source).get("model_provider") or ""),
            "to_provider": str(item.get("from_provider") or ""),
            "sha256": sha256(destination),
        })
    database_backup = backup_dir / "state_5.sqlite"
    copy_sqlite_snapshot(state_db, database_backup)
    manifest = {
        "format_version": 1, "operation": "pre_restore_backup",
        "created_at": datetime.now().astimezone().isoformat(), "codex_home": str(codex_home),
        "state_db": str(state_db), "database_backup": database_backup.name,
        "database_sha256": sha256(database_backup), "target_provider": "pre_restore_snapshot",
        "sessions": manifest_items,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_backup_info(backup_dir, codex_home)


def apply_backup_snapshot(backup: BackupInfo, codex_home: Path, state_db: Path) -> None:
    for item in manifest_sessions(backup.manifest):
        source = backup.backup_dir / safe_relative_path(item.get("backup_path"), "backup_path")
        destination = codex_home / safe_relative_path(item.get("rollout_path"), "rollout_path")
        atomic_write_bytes(destination, source.read_bytes())
    database_backup = backup.backup_dir / safe_relative_path(backup.manifest.get("database_backup"), "database_backup")
    restore_database_snapshot(database_backup, state_db)


def verify_restored_backup(backup: BackupInfo, codex_home: Path, state_db: Path) -> None:
    threads = load_thread_records(state_db)
    for item in manifest_sessions(backup.manifest):
        source = backup.backup_dir / safe_relative_path(item.get("backup_path"), "backup_path")
        destination = codex_home / safe_relative_path(item.get("rollout_path"), "rollout_path")
        if sha256(source) != sha256(destination):
            raise ToolError(f"恢复后 JSONL 哈希不一致：{destination}")
        session_id, from_provider = item.get("session_id"), item.get("from_provider")
        if isinstance(session_id, str) and isinstance(from_provider, str) and from_provider:
            thread = threads.get(session_id)
            if read_session_meta(destination).get("model_provider") != from_provider or thread is None or thread.model_provider != from_provider:
                raise ToolError(f"恢复后 provider 校验失败：{session_id}")


def restore_backup(backup: BackupInfo, codex_home: Path, state_db: Path) -> Path:
    """恢复备份；若失败则从恢复前快照回退。"""
    current_backup = backup_current_state_for_restore(codex_home, state_db, backup)
    try:
        apply_backup_snapshot(backup, codex_home, state_db)
        verify_restored_backup(backup, codex_home, state_db)
    except Exception as error:
        try:
            apply_backup_snapshot(current_backup, codex_home, state_db)
        except Exception as rollback_error:
            raise ToolError(f"恢复失败且自动回退不完整，请使用恢复前备份：{current_backup.backup_dir}") from rollback_error
        if isinstance(error, ToolError):
            raise
        raise ToolError(f"恢复失败，已回退到恢复前状态：{error}") from error
    return current_backup.backup_dir
