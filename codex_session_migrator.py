#!/usr/bin/env python3
"""安全扫描 Codex 本地会话，并为 provider 迁移提供选择界面。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
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

    migrate_parser = subparsers.add_parser("migrate", help="选择待迁移会话（当前仅预览）")
    _add_scan_arguments(migrate_parser)

    restore_parser = subparsers.add_parser("restore", help="从迁移备份恢复会话（后续实现）")
    restore_parser.add_argument("--backup", type=Path, help="备份目录")
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

    print("\n已选择的会话（本阶段只读预览，不会写入）：")
    print_sessions(selected)
    print("\n下一阶段将要求选择目标 provider，并在确认后执行迁移。")
    print_issues(result.issues)
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
            print("restore 功能将在第 4 阶段实现。")
            return 0
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
