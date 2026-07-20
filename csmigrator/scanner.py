"""Codex JSONL 会话与 SQLite 线程记录的只读扫描。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .models import ParsedSession, ScanIssue, ScanResult, SessionRecord, ThreadRecord, ToolError


MAX_META_LINES = 20


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _readonly_sqlite(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ToolError(f"找不到 SQLite 数据库：{resolved}")
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


@contextmanager
def open_readonly_sqlite(path: Path) -> Iterator[sqlite3.Connection]:
    connection = _readonly_sqlite(path)
    try:
        yield connection
    finally:
        connection.close()


def thread_columns(path: Path) -> set[str]:
    with open_readonly_sqlite(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "threads" not in tables:
            raise ToolError(f"数据库不包含 threads 表：{path}")
        return {row[1] for row in connection.execute("PRAGMA table_info(threads)")}


def locate_state_db(codex_home: Path, explicit_path: Path | None = None) -> Path:
    """定位并验证活跃的 state_5.sqlite，根目录版本优先。"""
    if explicit_path is not None:
        candidate = explicit_path.expanduser().resolve()
        columns = thread_columns(candidate)
        if not {"id", "model_provider", "title"}.issubset(columns):
            raise ToolError(f"数据库缺少必要字段：{candidate}")
        return candidate

    candidates = [codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"]
    problems: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            columns = thread_columns(candidate)
        except (ToolError, sqlite3.Error) as error:
            problems.append(str(error))
            continue
        if {"id", "model_provider", "title"}.issubset(columns):
            return candidate.resolve()
        problems.append(f"数据库缺少必要字段：{candidate}")
    detail = "；".join(problems)
    raise ToolError(f"未找到可用的 state_5.sqlite{f'（{detail}）' if detail else ''}")


def load_thread_records(state_db: Path) -> dict[str, ThreadRecord]:
    """读取数据库中的线程 provider、标题与归档状态。"""
    columns = thread_columns(state_db)
    archived_column = "archived" if "archived" in columns else "0"
    working_directory_column = "cwd" if "cwd" in columns else "''"
    query = (
        "SELECT id, model_provider, title, "
        f"{working_directory_column} AS working_directory, {archived_column} AS archived "
        "FROM threads"
    )
    with open_readonly_sqlite(state_db) as connection:
        rows = connection.execute(query).fetchall()
    return {
        str(session_id): ThreadRecord(
            str(session_id),
            str(provider),
            str(title or ""),
            str(working_directory or ""),
            bool(archived),
        )
        for session_id, provider, title, working_directory, archived in rows
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
            session_id, title = record.get("id"), record.get("thread_name")
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


def read_session_meta(path: Path) -> dict[str, object]:
    """读取 rollout JSONL 前若干行中的 session_meta。"""
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
    raise ToolError(f"前 {MAX_META_LINES} 行内未找到 session_meta")


def _scan_rollout_files(sessions_root: Path, archived: bool) -> tuple[list[ParsedSession], list[ScanIssue]]:
    parsed: list[ParsedSession] = []
    issues: list[ScanIssue] = []
    if not sessions_root.is_dir():
        return parsed, issues
    for rollout_path in sorted(sessions_root.rglob("rollout-*.jsonl")):
        try:
            payload = read_session_meta(rollout_path)
        except (OSError, ToolError) as error:
            issues.append(ScanIssue("无法解析", str(error), path=rollout_path))
            continue
        session_id = payload.get("id") or payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            issues.append(ScanIssue("缺少 Session ID", "session_meta 未提供 id", rollout_path))
            continue
        timestamp = _parse_timestamp(payload.get("timestamp"))
        session_date = _date_from_path(rollout_path, sessions_root) or (
            timestamp.astimezone().date() if timestamp is not None else None
        )
        if session_date is None:
            issues.append(ScanIssue("缺少日期", "无法从路径或时间戳推断日期", rollout_path, session_id))
            continue
        provider = payload.get("model_provider")
        parsed.append(ParsedSession(session_id, timestamp, session_date, provider if isinstance(provider, str) and provider else None, rollout_path, archived))
    return parsed, issues


def scan_sessions(codex_home: Path | None = None, state_db: Path | None = None, include_archived: bool = False) -> ScanResult:
    """只读扫描 JSONL 和 SQLite，并标记不可安全迁移的记录。"""
    resolved_home = (codex_home or default_codex_home()).expanduser().resolve()
    if not resolved_home.is_dir():
        raise ToolError(f"找不到 Codex 数据目录：{resolved_home}")
    resolved_db = locate_state_db(resolved_home, state_db)
    threads, titles = load_thread_records(resolved_db), load_session_titles(resolved_home)
    parsed, issues = _scan_rollout_files(resolved_home / "sessions", archived=False)
    if include_archived:
        archived_parsed, archived_issues = _scan_rollout_files(resolved_home / "archived_sessions", archived=True)
        parsed.extend(archived_parsed)
        issues.extend(archived_issues)
    duplicate_ids = {identifier for identifier, count in Counter(item.session_id for item in parsed).items() if count > 1}
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
            issues.append(ScanIssue(status, f"会话不会进入迁移选择列表：{item.session_id}", item.rollout_path, item.session_id))
        sessions.append(SessionRecord(
            item.session_id, item.session_date, item.timestamp,
            titles.get(item.session_id) or (thread.title if thread else "") or "(无标题)",
            thread.working_directory if thread else "",
            item.model_provider, thread.model_provider if thread else None, item.rollout_path,
            item.archived or bool(thread and thread.archived), status,
        ))
    sessions.sort(key=lambda item: (item.session_date, item.timestamp.timestamp() if item.timestamp else 0), reverse=True)
    return ScanResult(resolved_home, resolved_db, tuple(sessions), tuple(issues))


def filter_sessions(sessions: Iterable[SessionRecord], date_from: date | None = None, date_to: date | None = None, provider: str | None = None, session_ids: Sequence[str] | None = None, include_archived: bool = False) -> list[SessionRecord]:
    """应用日期、provider、ID 与归档筛选。"""
    wanted_ids = set(session_ids or [])
    return [
        session for session in sessions
        if (include_archived or not session.archived)
        and (date_from is None or session.session_date >= date_from)
        and (date_to is None or session.session_date <= date_to)
        and (provider is None or session.provider == provider)
        and (not wanted_ids or session.session_id in wanted_ids)
    ]
