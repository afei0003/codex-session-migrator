#!/usr/bin/env python3
"""安全扫描 Codex 本地会话，并为 provider 迁移提供选择界面。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


PROGRAM_NAME = "codex-session-migrator"
VERSION = "0.2.0"
MAX_META_LINES = 20


class ToolError(RuntimeError):
    """向用户展示的可预期错误。"""


@dataclass(frozen=True)
class ThreadRecord:
    """`state_5.sqlite` 的线程摘要。"""

    session_id: str
    model_provider: str
    title: str
    archived: bool


@dataclass(frozen=True)
class ScanIssue:
    """会话扫描时发现的异常。"""

    code: str
    message: str
    path: Path | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class SessionRecord:
    """可显示的一条 Codex 会话记录。"""

    session_id: str
    session_date: date
    timestamp: datetime | None
    title: str
    json_provider: str | None
    database_provider: str | None
    rollout_path: Path
    archived: bool
    status: str

    @property
    def provider(self) -> str:
        """优先显示 JSONL provider，缺失时回退数据库值。"""
        return self.json_provider or self.database_provider or "-"

    @property
    def selectable(self) -> bool:
        """只有双端完整且一致的会话可在迁移阶段中选择。"""
        return self.status == "一致"


@dataclass(frozen=True)
class ScanResult:
    """一次扫描的只读结果。"""

    codex_home: Path
    state_db: Path
    sessions: tuple[SessionRecord, ...]
    issues: tuple[ScanIssue, ...]


@dataclass(frozen=True)
class MigrationResult:
    """一次成功迁移的备份和变更摘要。"""

    backup_dir: Path
    session_ids: tuple[str, ...]
    target_provider: str


@dataclass(frozen=True)
class BackupInfo:
    """已验证、可用于恢复的一份迁移备份。"""

    backup_dir: Path
    manifest: dict[str, object]


@dataclass(frozen=True)
class _ParsedSession:
    """尚未与 SQLite 合并的 JSONL 元数据。"""

    session_id: str
    timestamp: datetime | None
    session_date: date
    model_provider: str | None
    rollout_path: Path
    archived: bool


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD 格式") from error


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_codex_home() -> Path:
    return Path.home() / ".codex"


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ToolError(f"找不到 SQLite 数据库：{resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


@contextmanager
def open_readonly_sqlite(path: Path) -> Iterable[sqlite3.Connection]:
    connection = _readonly_sqlite(path)
    try:
        yield connection
    finally:
        connection.close()


def _thread_columns(path: Path) -> set[str]:
    with open_readonly_sqlite(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "threads" not in tables:
            raise ToolError(f"数据库不包含 threads 表：{path}")
        return {
            row[1]
            for row in connection.execute("PRAGMA table_info(threads)")
        }


def locate_state_db(codex_home: Path, explicit_path: Path | None = None) -> Path:
    """定位并验证活跃的 state_5.sqlite，根目录版本优先。"""
    if explicit_path is not None:
        candidate = explicit_path.expanduser().resolve()
        columns = _thread_columns(candidate)
        if not {"id", "model_provider", "title"}.issubset(columns):
            raise ToolError(f"数据库缺少必要字段：{candidate}")
        return candidate

    candidates = [codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"]
    problems: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            columns = _thread_columns(candidate)
        except (ToolError, sqlite3.Error) as error:
            problems.append(str(error))
            continue
        if {"id", "model_provider", "title"}.issubset(columns):
            return candidate.resolve()
        problems.append(f"数据库缺少必要字段：{candidate}")

    detail = "；".join(problems)
    suffix = f"（{detail}）" if detail else ""
    raise ToolError(f"未找到可用的 state_5.sqlite{suffix}")


def load_thread_records(state_db: Path) -> dict[str, ThreadRecord]:
    """读取数据库中的线程 provider、标题与归档状态。"""
    columns = _thread_columns(state_db)
    archived_column = "archived" if "archived" in columns else "0"
    query = (
        "SELECT id, model_provider, title, "
        f"{archived_column} AS archived FROM threads"
    )
    with open_readonly_sqlite(state_db) as connection:
        rows = connection.execute(query).fetchall()
    return {
        str(session_id): ThreadRecord(
            session_id=str(session_id),
            model_provider=str(provider),
            title=str(title or ""),
            archived=bool(archived),
        )
        for session_id, provider, title, archived in rows
    }


def load_session_titles(codex_home: Path) -> dict[str, str]:
    """读取 session_index.jsonl；同一 ID 的最后一条记录优先。"""
    index_path = codex_home / "session_index.jsonl"
    if not index_path.is_file():
        return {}

    titles: dict[str, str] = {}
    with index_path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = record.get("id")
            title = record.get("thread_name")
            if isinstance(session_id, str) and isinstance(title, str) and title.strip():
                titles[session_id] = title.strip()
    return titles


def _date_from_path(path: Path, sessions_root: Path) -> date | None:
    try:
        parts = path.relative_to(sessions_root).parts
    except ValueError:
        return None
    if len(parts) < 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _read_session_meta(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8-sig") as file:
        for _ in range(MAX_META_LINES):
            line = file.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ToolError(f"JSONL 首行无法解析：{error.msg}") from error
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise ToolError("session_meta 缺少对象类型的 payload")
            return payload
    raise ToolError("前 20 行内未找到 session_meta")


def _scan_rollout_files(
    sessions_root: Path,
    archived: bool,
) -> tuple[list[_ParsedSession], list[ScanIssue]]:
    parsed: list[_ParsedSession] = []
    issues: list[ScanIssue] = []
    if not sessions_root.is_dir():
        return parsed, issues

    for rollout_path in sorted(sessions_root.rglob("rollout-*.jsonl")):
        try:
            payload = _read_session_meta(rollout_path)
        except (OSError, ToolError) as error:
            issues.append(
                ScanIssue("无法解析", str(error), path=rollout_path)
            )
            continue

        session_id = payload.get("id") or payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            issues.append(ScanIssue("缺少 Session ID", "session_meta 未提供 id", rollout_path))
            continue

        timestamp = _parse_timestamp(payload.get("timestamp"))
        session_date = _date_from_path(rollout_path, sessions_root)
        if session_date is None and timestamp is not None:
            session_date = timestamp.astimezone().date()
        if session_date is None:
            issues.append(
                ScanIssue("缺少日期", "无法从路径或时间戳推断日期", rollout_path, session_id)
            )
            continue

        provider = payload.get("model_provider")
        parsed.append(
            _ParsedSession(
                session_id=session_id,
                timestamp=timestamp,
                session_date=session_date,
                model_provider=provider if isinstance(provider, str) and provider else None,
                rollout_path=rollout_path,
                archived=archived,
            )
        )
    return parsed, issues


def scan_sessions(
    codex_home: Path | None = None,
    state_db: Path | None = None,
    include_archived: bool = False,
) -> ScanResult:
    """只读扫描 JSONL 和 SQLite，并标记不可安全迁移的记录。"""
    resolved_home = (codex_home or _default_codex_home()).expanduser().resolve()
    if not resolved_home.is_dir():
        raise ToolError(f"找不到 Codex 数据目录：{resolved_home}")

    resolved_db = locate_state_db(resolved_home, state_db)
    threads = load_thread_records(resolved_db)
    titles = load_session_titles(resolved_home)

    parsed, issues = _scan_rollout_files(resolved_home / "sessions", archived=False)
    if include_archived:
        archived_parsed, archived_issues = _scan_rollout_files(
            resolved_home / "archived_sessions", archived=True
        )
        parsed.extend(archived_parsed)
        issues.extend(archived_issues)

    duplicate_ids = {
        session_id
        for session_id, count in Counter(item.session_id for item in parsed).items()
        if count > 1
    }
    sessions: list[SessionRecord] = []
    for item in parsed:
        thread = threads.get(item.session_id)
        if item.session_id in duplicate_ids:
            status = "重复 Session ID"
        elif thread is None:
            status = "数据库缺失"
        elif item.model_provider is None:
            status = "JSONL 缺少 provider"
        elif item.model_provider != thread.model_provider:
            status = "Provider 不一致"
        else:
            status = "一致"

        if status != "一致":
            issues.append(
                ScanIssue(status, f"会话不会进入迁移选择列表：{item.session_id}", item.rollout_path, item.session_id)
            )

        database_provider = thread.model_provider if thread else None
        title = titles.get(item.session_id) or (thread.title if thread else "") or "(无标题)"
        sessions.append(
            SessionRecord(
                session_id=item.session_id,
                session_date=item.session_date,
                timestamp=item.timestamp,
                title=title,
                json_provider=item.model_provider,
                database_provider=database_provider,
                rollout_path=item.rollout_path,
                archived=item.archived or bool(thread and thread.archived),
                status=status,
            )
        )

    sessions.sort(
        key=lambda item: (
            item.session_date,
            item.timestamp.timestamp() if item.timestamp is not None else 0,
        ),
        reverse=True,
    )
    return ScanResult(resolved_home, resolved_db, tuple(sessions), tuple(issues))


def filter_sessions(
    sessions: Iterable[SessionRecord],
    date_from: date | None = None,
    date_to: date | None = None,
    provider: str | None = None,
    session_ids: Sequence[str] | None = None,
    include_archived: bool = False,
) -> list[SessionRecord]:
    """应用日期、provider、ID 与归档筛选。"""
    wanted_ids = set(session_ids or [])
    selected: list[SessionRecord] = []
    for session in sessions:
        if not include_archived and session.archived:
            continue
        if date_from and session.session_date < date_from:
            continue
        if date_to and session.session_date > date_to:
            continue
        if provider and session.provider != provider:
            continue
        if wanted_ids and session.session_id not in wanted_ids:
            continue
        selected.append(session)
    return selected


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
            start = int(bounds[0])
            end = int(bounds[-1])
        except ValueError as error:
            raise ToolError("请选择编号，例如 1,3-5") from error
        if start < 1 or end < 1 or start > end or end > maximum:
            raise ToolError(f"编号必须在 1 到 {maximum} 之间")
        indexes.extend(range(start, end + 1))
    return list(dict.fromkeys(indexes))


def _short_title(value: str, limit: int = 54) -> str:
    flattened = " ".join(value.split())
    return flattened if len(flattened) <= limit else f"{flattened[: limit - 1]}…"


def print_sessions(sessions: Sequence[SessionRecord]) -> None:
    """按日期打印会话表。"""
    if not sessions:
        print("没有符合条件的会话。")
        return
    for index, session in enumerate(sessions, start=1):
        archived = " [已归档]" if session.archived else ""
        print(
            f"[{index:>3}] {session.session_date.isoformat()} | {session.session_id} | "
            f"{session.provider} | {session.status}{archived} | {_short_title(session.title)}"
        )


def print_issues(issues: Sequence[ScanIssue]) -> None:
    if not issues:
        return
    print(f"\n发现 {len(issues)} 项异常（只报告，不进入迁移选择）：")
    for issue in issues:
        location = f" | {issue.path}" if issue.path else ""
        print(f"- {issue.code}：{issue.message}{location}")


def choose_sessions_interactively(
    sessions: Sequence[SessionRecord],
    input_fn: Callable[[str], str] = input,
) -> list[SessionRecord]:
    """先选择日期，再选择对应会话；始终拒绝一键全选。"""
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
    session_indexes = parse_number_selection(
        input_fn("选择会话编号（例如 1,3-5）："), len(candidates)
    )
    return [candidates[index - 1] for index in session_indexes]


def _configured_providers(codex_home: Path) -> set[str]:
    """从 config.toml 提取已配置 provider；配置不可读时保留扫描结果。"""
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
        providers.update(
            name.strip()
            for name in configured
            if isinstance(name, str) and name.strip()
        )
    return providers


def discover_providers(result: ScanResult) -> list[str]:
    """汇总配置、JSONL 与数据库中的精确 provider 名称。"""
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


def choose_target_provider(
    providers: Sequence[str], input_fn: Callable[[str], str] = input
) -> str:
    """让用户从已发现 provider 中选择或手工输入新值。"""
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
    """输出逐条 provider 变更预览。"""
    print("\n迁移预览：")
    for index, session in enumerate(sessions, start=1):
        print(
            f"[{index:>3}] {session.session_id} | {_short_title(session.title)} | "
            f"{session.provider} -> {target_provider}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    """使用 SQLite Backup API 生成包含 WAL 数据的一致快照。"""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with open_readonly_sqlite(source_path) as source:
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """在目标同目录落盘后原子替换，避免生成半截 JSONL。"""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _updated_session_json(path: Path, expected_provider: str, target_provider: str) -> bytes:
    """只更新 session_meta 的 provider，其余 JSONL 行保持原始字节不变。"""
    lines = path.read_bytes().splitlines(keepends=True)
    for index, original_line in enumerate(lines[:MAX_META_LINES]):
        if not original_line.strip():
            continue
        has_bom = index == 0 and original_line.startswith(b"\xef\xbb\xbf")
        decoded = original_line.decode("utf-8-sig" if has_bom else "utf-8")
        try:
            record = json.loads(decoded)
        except json.JSONDecodeError as error:
            raise ToolError(f"无法更新 JSONL：{path}：{error.msg}") from error
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ToolError(f"无法更新 JSONL：session_meta payload 无效：{path}")
        current_provider = payload.get("model_provider")
        if current_provider != expected_provider:
            raise ToolError(
                f"JSONL provider 已变化，拒绝覆盖：{path}（当前为 {current_provider!r}）"
            )
        payload["model_provider"] = target_provider
        suffix = b"\r\n" if original_line.endswith(b"\r\n") else b"\n"
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        lines[index] = (b"\xef\xbb\xbf" if has_bom else b"") + encoded + suffix
        return b"".join(lines)
    raise ToolError(f"无法更新 JSONL：前 {MAX_META_LINES} 行内未找到 session_meta：{path}")


def _backup_selected_sessions(
    result: ScanResult,
    sessions: Sequence[SessionRecord],
    target_provider: str,
) -> Path:
    """保存选中 JSONL、数据库一致快照和恢复所需清单。"""
    backup_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    backup_dir = result.codex_home / "backups" / "provider-migrations" / backup_id
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
        manifest_sessions.append(
            {
                "session_id": session.session_id,
                "rollout_path": relative_path.as_posix(),
                "backup_path": destination.relative_to(backup_dir).as_posix(),
                "from_provider": session.provider,
                "to_provider": target_provider,
                "sha256": _sha256(destination),
            }
        )

    database_backup = backup_dir / "state_5.sqlite"
    _copy_sqlite_snapshot(result.state_db, database_backup)
    manifest = {
        "format_version": 1,
        "created_at": datetime.now().astimezone().isoformat(),
        "codex_home": str(result.codex_home),
        "state_db": str(result.state_db),
        "database_backup": database_backup.name,
        "database_sha256": _sha256(database_backup),
        "target_provider": target_provider,
        "sessions": manifest_sessions,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return backup_dir


def _restore_jsonl_from_backup(backup_dir: Path) -> None:
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["sessions"]:
        source = backup_dir / item["backup_path"]
        destination = Path(manifest["codex_home"]) / item["rollout_path"]
        _atomic_write_bytes(destination, source.read_bytes())


def _restore_database_snapshot(backup_db: Path, state_db: Path) -> None:
    source = _readonly_sqlite(backup_db)
    destination = sqlite3.connect(state_db)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def migrate_sessions(
    result: ScanResult,
    sessions: Sequence[SessionRecord],
    target_provider: str,
) -> MigrationResult:
    """备份并同步更新选中 JSONL 与 SQLite；失败时回退到备份。"""
    if not sessions:
        raise ToolError("没有可迁移的会话")
    target_provider = validate_provider(target_provider)
    invalid = [session.session_id for session in sessions if not session.selectable]
    if invalid:
        raise ToolError(f"异常会话不能迁移：{', '.join(invalid)}")

    updates = {
        session.rollout_path: _updated_session_json(
            session.rollout_path, session.provider, target_provider
        )
        for session in sessions
    }
    backup_dir = _backup_selected_sessions(result, sessions, target_provider)
    replaced_jsonl = False
    connection = sqlite3.connect(result.state_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for session in sessions:
            cursor = connection.execute(
                "UPDATE threads SET model_provider = ? "
                "WHERE id = ? AND model_provider = ?",
                (target_provider, session.session_id, session.provider),
            )
            if cursor.rowcount != 1:
                raise ToolError(f"数据库 provider 已变化，拒绝覆盖：{session.session_id}")
        for path, content in updates.items():
            _atomic_write_bytes(path, content)
            replaced_jsonl = True
        connection.commit()
        _verify_migration(result.state_db, sessions, target_provider)
    except Exception as error:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        restore_errors: list[str] = []
        if replaced_jsonl:
            try:
                _restore_jsonl_from_backup(backup_dir)
            except (OSError, ToolError, json.JSONDecodeError) as restore_error:
                restore_errors.append(f"JSONL 恢复失败：{restore_error}")
        try:
            _restore_database_snapshot(backup_dir / "state_5.sqlite", result.state_db)
        except (OSError, sqlite3.Error, ToolError) as restore_error:
            restore_errors.append(f"SQLite 恢复失败：{restore_error}")
        if restore_errors:
            detail = "；".join(restore_errors)
            raise ToolError(
                f"迁移失败，自动回退不完整，请使用备份目录手工恢复：{backup_dir}（{detail}）"
            ) from error
        if isinstance(error, ToolError):
            raise
        raise ToolError(f"迁移失败，已从备份回退：{error}") from error
    finally:
        connection.close()
    return MigrationResult(backup_dir, tuple(session.session_id for session in sessions), target_provider)


def _verify_migration(
    state_db: Path, sessions: Sequence[SessionRecord], target_provider: str
) -> None:
    threads = load_thread_records(state_db)
    for session in sessions:
        payload = _read_session_meta(session.rollout_path)
        json_provider = payload.get("model_provider")
        db_provider = threads.get(session.session_id)
        if json_provider != target_provider or db_provider is None or db_provider.model_provider != target_provider:
            raise ToolError(f"写入后验证失败：{session.session_id}")


def find_running_codex_processes() -> list[str]:
    """返回可能写入 Codex 数据的客户端进程名称。"""
    candidates = {"codex", "codex.exe", "codex-desktop", "codex-desktop.exe"}
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode == 0:
                names = [row[0] for row in csv.reader(completed.stdout.splitlines()) if row]
            else:
                fallback = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        "Get-Process | Select-Object -ExpandProperty ProcessName",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if fallback.returncode != 0:
                    raise ToolError("无法检测 Codex 进程，拒绝写入以保护会话数据")
                names = [line.strip() for line in fallback.stdout.splitlines() if line.strip()]
                completed = fallback
        else:
            completed = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
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


def _migration_backups_root(codex_home: Path) -> Path:
    return codex_home / "backups" / "provider-migrations"


def _safe_relative_path(value: object, description: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ToolError(f"备份清单缺少 {description}")
    path = Path(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ToolError(f"备份清单中的 {description} 不是安全相对路径：{value}")
    return path


def _manifest_sessions(manifest: dict[str, object]) -> list[dict[str, object]]:
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise ToolError("备份清单不包含会话文件")
    if not all(isinstance(session, dict) for session in sessions):
        raise ToolError("备份清单包含无效会话项")
    return sessions


def load_backup_info(backup_dir: Path, codex_home: Path) -> BackupInfo:
    """加载并验证备份清单、哈希和恢复目标，拒绝跨目录恢复。"""
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

    database_name = _safe_relative_path(manifest.get("database_backup"), "database_backup")
    database_path = resolved_dir / database_name
    if not database_path.is_file():
        raise ToolError(f"备份数据库不存在：{database_path}")
    expected_database_hash = manifest.get("database_sha256")
    if not isinstance(expected_database_hash, str) or _sha256(database_path) != expected_database_hash:
        raise ToolError(f"备份数据库哈希校验失败：{database_path}")

    for session in _manifest_sessions(manifest):
        source = resolved_dir / _safe_relative_path(session.get("backup_path"), "backup_path")
        destination = codex_home / _safe_relative_path(session.get("rollout_path"), "rollout_path")
        expected_hash = session.get("sha256")
        if not source.is_file() or not isinstance(expected_hash, str) or _sha256(source) != expected_hash:
            raise ToolError(f"会话备份哈希校验失败：{source}")
        if not destination.is_file():
            raise ToolError(f"当前会话文件不存在，拒绝覆盖：{destination}")
    return BackupInfo(resolved_dir, manifest)


def list_backups(codex_home: Path) -> list[BackupInfo]:
    """列出可验证、可恢复的备份；损坏备份不会进入选择菜单。"""
    root = _migration_backups_root(codex_home)
    if not root.is_dir():
        return []
    backups: list[BackupInfo] = []
    for directory in sorted((path for path in root.iterdir() if path.is_dir()), reverse=True):
        try:
            backups.append(load_backup_info(directory, codex_home))
        except ToolError:
            continue
    return backups


def choose_backup_interactively(
    backups: Sequence[BackupInfo], input_fn: Callable[[str], str] = input
) -> BackupInfo:
    if not backups:
        raise ToolError("没有找到可恢复的有效备份")
    print("可恢复备份：")
    for index, backup in enumerate(backups, start=1):
        created_at = backup.manifest.get("created_at", "未知时间")
        sessions = backup.manifest.get("sessions", [])
        target = backup.manifest.get("target_provider", "未知 provider")
        print(f"[{index}] {backup.backup_dir.name} | {created_at} | {len(sessions)} 条 | {target}")
    indexes = parse_number_selection(input_fn("选择一个备份编号："), len(backups))
    if len(indexes) != 1:
        raise ToolError("一次只能恢复一个备份")
    return backups[indexes[0] - 1]


def _backup_current_state_for_restore(
    codex_home: Path, state_db: Path, source_backup: BackupInfo
) -> BackupInfo:
    """在恢复前保存当前状态，以便恢复失败时回退。"""
    backup_id = f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')}-before-restore"
    backup_dir = _migration_backups_root(codex_home) / backup_id
    backup_dir.mkdir(parents=True, exist_ok=False)
    sessions_root = backup_dir / "sessions"
    manifest_sessions: list[dict[str, str]] = []

    for item in _manifest_sessions(source_backup.manifest):
        rollout_relative = _safe_relative_path(item.get("rollout_path"), "rollout_path")
        source = codex_home / rollout_relative
        destination = sessions_root / rollout_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        session_id = item.get("session_id")
        manifest_sessions.append(
            {
                "session_id": str(session_id or ""),
                "rollout_path": rollout_relative.as_posix(),
                "backup_path": destination.relative_to(backup_dir).as_posix(),
                "from_provider": str(_read_session_meta(source).get("model_provider") or ""),
                "to_provider": str(item.get("from_provider") or ""),
                "sha256": _sha256(destination),
            }
        )

    database_backup = backup_dir / "state_5.sqlite"
    _copy_sqlite_snapshot(state_db, database_backup)
    manifest = {
        "format_version": 1,
        "operation": "pre_restore_backup",
        "created_at": datetime.now().astimezone().isoformat(),
        "codex_home": str(codex_home),
        "state_db": str(state_db),
        "database_backup": database_backup.name,
        "database_sha256": _sha256(database_backup),
        "target_provider": "pre_restore_snapshot",
        "sessions": manifest_sessions,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return load_backup_info(backup_dir, codex_home)


def _apply_backup_snapshot(backup: BackupInfo, codex_home: Path, state_db: Path) -> None:
    """写入一份已经验证的备份，不创建额外备份。"""
    for item in _manifest_sessions(backup.manifest):
        source = backup.backup_dir / _safe_relative_path(item.get("backup_path"), "backup_path")
        destination = codex_home / _safe_relative_path(item.get("rollout_path"), "rollout_path")
        _atomic_write_bytes(destination, source.read_bytes())
    database_backup = backup.backup_dir / _safe_relative_path(
        backup.manifest.get("database_backup"), "database_backup"
    )
    _restore_database_snapshot(database_backup, state_db)


def _verify_restored_backup(backup: BackupInfo, codex_home: Path, state_db: Path) -> None:
    """校验恢复后的 JSONL 哈希及 Session provider。"""
    threads = load_thread_records(state_db)
    for item in _manifest_sessions(backup.manifest):
        source = backup.backup_dir / _safe_relative_path(item.get("backup_path"), "backup_path")
        destination = codex_home / _safe_relative_path(item.get("rollout_path"), "rollout_path")
        if _sha256(source) != _sha256(destination):
            raise ToolError(f"恢复后 JSONL 哈希不一致：{destination}")
        session_id = item.get("session_id")
        from_provider = item.get("from_provider")
        if isinstance(session_id, str) and isinstance(from_provider, str) and from_provider:
            payload = _read_session_meta(destination)
            thread = threads.get(session_id)
            if payload.get("model_provider") != from_provider or thread is None or thread.model_provider != from_provider:
                raise ToolError(f"恢复后 provider 校验失败：{session_id}")


def restore_backup(backup: BackupInfo, codex_home: Path, state_db: Path) -> Path:
    """恢复备份；若失败则从恢复前快照回退。"""
    current_backup = _backup_current_state_for_restore(codex_home, state_db, backup)
    try:
        _apply_backup_snapshot(backup, codex_home, state_db)
        _verify_restored_backup(backup, codex_home, state_db)
    except Exception as error:
        restore_errors: list[str] = []
        try:
            _apply_backup_snapshot(current_backup, codex_home, state_db)
        except Exception as restore_error:
            restore_errors.append(str(restore_error))
        if restore_errors:
            raise ToolError(
                f"恢复失败且自动回退不完整，请使用恢复前备份：{current_backup.backup_dir}"
            ) from error
        if isinstance(error, ToolError):
            raise
        raise ToolError(f"恢复失败，已回退到恢复前状态：{error}") from error
    return current_backup.backup_dir


def print_restore_preview(backup: BackupInfo) -> None:
    created_at = backup.manifest.get("created_at", "未知时间")
    target = backup.manifest.get("target_provider", "未知 provider")
    sessions = backup.manifest.get("sessions", [])
    print("恢复预览：")
    print(f"- 备份：{backup.backup_dir.name}")
    print(f"- 创建时间：{created_at}")
    print(f"- 会话数量：{len(sessions)}")
    print(f"- 备份目标 provider：{target}")
    for item in sessions:
        if isinstance(item, dict):
            print(
                f"  - {item.get('session_id', '未知 Session ID')} | "
                f"恢复为 {item.get('from_provider', '未知 provider')}"
            )


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", type=Path, help="Codex 数据目录，默认 ~/.codex")
    parser.add_argument("--state-db", type=Path, help="显式指定 state_5.sqlite")
    parser.add_argument("--from", dest="date_from", type=_parse_date, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", type=_parse_date, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--provider", help="按当前 provider 精确筛选（区分大小写）")
    parser.add_argument("--session-id", nargs="+", help="按一个或多个 Session ID 筛选")
    parser.add_argument("--include-archived", action="store_true", help="包含已归档会话")


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="安全扫描、选择并迁移 Codex 会话的 model_provider。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    list_parser = subparsers.add_parser("list", help="扫描并列出 Codex 会话")
    _add_scan_arguments(list_parser)

    migrate_parser = subparsers.add_parser("migrate", help="预览并迁移会话 provider")
    _add_scan_arguments(migrate_parser)
    migrate_parser.add_argument("--to-provider", help="目标 provider（区分大小写）")
    migrate_parser.add_argument("--dry-run", action="store_true", help="仅预览，不检查进程且不写入")

    restore_parser = subparsers.add_parser("restore", help="从迁移备份恢复会话")
    restore_parser.add_argument("--codex-home", type=Path, help="Codex 数据目录，默认 ~/.codex")
    restore_parser.add_argument("--state-db", type=Path, help="显式指定 state_5.sqlite")
    restore_parser.add_argument("--backup", type=Path, help="备份目录")
    restore_parser.add_argument("--dry-run", action="store_true", help="仅校验并预览，不检查进程且不写入")
    return parser


def _scan_from_args(args: argparse.Namespace) -> tuple[ScanResult, list[SessionRecord]]:
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise ToolError("--from 不能晚于 --to")
    result = scan_sessions(args.codex_home, args.state_db, args.include_archived)
    filtered = filter_sessions(
        result.sessions,
        date_from=args.date_from,
        date_to=args.date_to,
        provider=args.provider,
        session_ids=args.session_id,
        include_archived=args.include_archived,
    )
    if args.session_id:
        found_ids = {session.session_id for session in result.sessions}
        missing = [session_id for session_id in args.session_id if session_id not in found_ids]
        if missing:
            raise ToolError(f"未找到 Session ID：{', '.join(missing)}")
    return result, filtered


def run_list(args: argparse.Namespace) -> int:
    result, filtered = _scan_from_args(args)
    print(f"Codex 目录：{result.codex_home}")
    print(f"活动数据库：{result.state_db}")
    print(f"扫描到 {len(result.sessions)} 条会话，筛选后 {len(filtered)} 条。\n")
    print_sessions(filtered)
    print_issues(result.issues)
    return 0


def run_migrate_preview(args: argparse.Namespace) -> int:
    result, filtered = _scan_from_args(args)
    if args.session_id:
        selected = [session for session in filtered if session.selectable]
        unselectable = [session for session in filtered if not session.selectable]
        if unselectable:
            identifiers = ", ".join(session.session_id for session in unselectable)
            raise ToolError(f"以下会话状态异常，不能选择：{identifiers}")
        if not selected:
            raise ToolError("没有可选择的会话")
    else:
        print(f"Codex 目录：{result.codex_home}")
        print(f"活动数据库：{result.state_db}\n")
        selected = choose_sessions_interactively(filtered)

    target_provider = (
        validate_provider(args.to_provider)
        if args.to_provider
        else choose_target_provider(discover_providers(result))
    )
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
    codex_home = (args.codex_home or _default_codex_home()).expanduser().resolve()
    if not codex_home.is_dir():
        raise ToolError(f"找不到 Codex 数据目录：{codex_home}")
    state_db = locate_state_db(codex_home, args.state_db)
    backup = (
        load_backup_info(args.backup, codex_home)
        if args.backup
        else choose_backup_interactively(list_backups(codex_home))
    )
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
    """运行命令行入口。无子命令时进入交互选择。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        raw_args = ["migrate"]
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


if __name__ == "__main__":
    raise SystemExit(main())
