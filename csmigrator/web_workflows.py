"""供 Web 界面调用的无框架业务编排。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .migration import migrate_sessions
from .models import BackupInfo, ScanResult, SessionRecord, ToolError
from .process_guard import ensure_codex_not_running
from .restore import list_backups, load_backup_info, restore_backup
from .scanner import default_codex_home, filter_sessions, locate_state_db, scan_sessions
from .selection import discover_providers, short_title, validate_provider


@dataclass(frozen=True)
class ScanRequest:
    """Web 表单提交的会话扫描条件。"""

    codex_home: Path
    state_db: Path | None
    date_from: date | None
    date_to: date | None
    provider: str | None
    session_ids: tuple[str, ...]
    include_archived: bool


@dataclass(frozen=True)
class WebScanResult:
    """适合表格渲染的扫描结果。"""

    request: ScanRequest
    result: ScanResult
    sessions: tuple[SessionRecord, ...]

    @property
    def rows(self) -> list[dict[str, object]]:
        return [
            {
                "选择": False,
                "日期": session.session_date.isoformat(),
                "Session ID": session.session_id,
                "标题": short_title(session.title, limit=20),
                "当前 provider": session.provider,
                "状态": session.status,
                "可迁移": session.selectable,
                "已归档": session.archived,
                "工作目录": session.working_directory or "(未知)",
            }
            for session in self.sessions
        ]

    @property
    def providers(self) -> list[str]:
        return discover_providers(self.result)

    @property
    def issues(self) -> list[str]:
        return [
            f"{issue.code}：{issue.message}"
            + (f" | {issue.path}" if issue.path else "")
            for issue in self.result.issues
        ]


@dataclass(frozen=True)
class MigrationPreview:
    """迁移预览及真正执行时所需的不可变条件。"""

    request: ScanRequest
    session_ids: tuple[str, ...]
    source_providers: tuple[tuple[str, str], ...]
    target_provider: str
    rows: tuple[dict[str, str], ...]

    @property
    def expected_confirmation(self) -> str:
        return f"MIGRATE {len(self.session_ids)}"


@dataclass(frozen=True)
class RestorePreview:
    """恢复预览及真正执行时所需的不可变条件。"""

    codex_home: Path
    state_db: Path
    backup_dir: Path
    backup_id: str
    rows: tuple[dict[str, str], ...]
    created_at: str
    target_provider: str

    @property
    def expected_confirmation(self) -> str:
        return f"RESTORE {self.backup_id}"


@dataclass(frozen=True)
class ActionResult:
    """页面操作的可展示结果。"""

    success: bool
    message: str
    backup_dir: Path | None = None


def _optional_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return Path(rendered).expanduser() if rendered else None


def _optional_date(value: str | date | None, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError as error:
            raise ToolError(f"{label} 必须使用 YYYY-MM-DD 格式") from error


def parse_session_ids(value: str | None) -> tuple[str, ...]:
    """解析文本框中的逗号、空格或换行分隔的 Session ID。"""
    if not value:
        return ()
    normalized = value.replace(",", " ").replace("\n", " ")
    return tuple(dict.fromkeys(item for item in normalized.split() if item))


def build_scan_request(
    codex_home: str | Path | None = None,
    state_db: str | Path | None = None,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    provider: str | None = None,
    session_ids: str | None = None,
    include_archived: bool = False,
) -> ScanRequest:
    resolved_home = (_optional_path(codex_home) or default_codex_home()).resolve()
    parsed_from = _optional_date(date_from, "起始日期")
    parsed_to = _optional_date(date_to, "结束日期")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise ToolError("起始日期不能晚于结束日期")
    cleaned_provider = provider.strip() if provider and provider.strip() else None
    return ScanRequest(
        codex_home=resolved_home,
        state_db=_optional_path(state_db),
        date_from=parsed_from,
        date_to=parsed_to,
        provider=cleaned_provider,
        session_ids=parse_session_ids(session_ids),
        include_archived=include_archived,
    )


def scan_for_web(request: ScanRequest) -> WebScanResult:
    result = scan_sessions(request.codex_home, request.state_db, request.include_archived)
    sessions = filter_sessions(
        result.sessions,
        date_from=request.date_from,
        date_to=request.date_to,
        provider=request.provider,
        session_ids=request.session_ids,
        include_archived=request.include_archived,
    )
    if request.session_ids:
        found_ids = {session.session_id for session in result.sessions}
        missing = [session_id for session_id in request.session_ids if session_id not in found_ids]
        if missing:
            raise ToolError(f"未找到 Session ID：{', '.join(missing)}")
    return WebScanResult(request, result, tuple(sessions))


def _selected_sessions(web_scan: WebScanResult, selected_ids: list[str] | tuple[str, ...]) -> list[SessionRecord]:
    wanted_ids = tuple(dict.fromkeys(session_id for session_id in selected_ids if session_id))
    if not wanted_ids:
        raise ToolError("请至少勾选一个会话")
    by_id = {session.session_id: session for session in web_scan.sessions}
    missing = [session_id for session_id in wanted_ids if session_id not in by_id]
    if missing:
        raise ToolError(f"所选会话已不在当前筛选结果中：{', '.join(missing)}")
    selected = [by_id[session_id] for session_id in wanted_ids]
    invalid = [session.session_id for session in selected if not session.selectable]
    if invalid:
        raise ToolError(f"以下会话状态异常，不能迁移：{', '.join(invalid)}")
    return selected


def prepare_migration(
    web_scan: WebScanResult,
    selected_ids: list[str] | tuple[str, ...],
    target_provider: str,
) -> MigrationPreview:
    """生成预览；不写入数据。"""
    target = validate_provider(target_provider)
    selected = _selected_sessions(web_scan, selected_ids)
    changes = [session for session in selected if session.provider != target]
    if not changes:
        raise ToolError("所选会话已属于目标 provider，无需迁移")
    return MigrationPreview(
        request=web_scan.request,
        session_ids=tuple(session.session_id for session in changes),
        source_providers=tuple((session.session_id, session.provider) for session in changes),
        target_provider=target,
        rows=tuple(
            {
                "Session ID": session.session_id,
                "标题": session.title,
                "当前 provider": session.provider,
                "目标 provider": target,
                "工作目录": session.working_directory or "(未知)",
            }
            for session in changes
        ),
    )


def execute_migration(preview: MigrationPreview, confirmation: str) -> ActionResult:
    """重新扫描后执行迁移，拒绝过期预览和不匹配确认短语。"""
    if confirmation.strip() != preview.expected_confirmation:
        return ActionResult(False, f"确认短语不匹配，请输入：{preview.expected_confirmation}")
    fresh_scan = scan_for_web(preview.request)
    selected = _selected_sessions(fresh_scan, preview.session_ids)
    expected_sources = dict(preview.source_providers)
    changed = [
        session.session_id
        for session in selected
        if expected_sources.get(session.session_id) != session.provider
    ]
    if changed:
        raise ToolError(f"会话 provider 已变化，请重新扫描并生成预览：{', '.join(changed)}")
    ensure_codex_not_running()
    migration = migrate_sessions(fresh_scan.result, selected, preview.target_provider)
    return ActionResult(
        True,
        f"迁移完成，JSONL 与 SQLite 已通过写后校验。备份目录：{migration.backup_dir}",
        migration.backup_dir,
    )


def list_backups_for_web(
    codex_home: str | Path | None = None,
    state_db: str | Path | None = None,
) -> tuple[Path, Path, list[dict[str, Any]]]:
    resolved_home = (_optional_path(codex_home) or default_codex_home()).resolve()
    if not resolved_home.is_dir():
        raise ToolError(f"找不到 Codex 数据目录：{resolved_home}")
    resolved_db = locate_state_db(resolved_home, _optional_path(state_db))
    backups = list_backups(resolved_home)
    rows = [
        {
            "备份 ID": backup.backup_dir.name,
            "备份目录": str(backup.backup_dir),
            "创建时间": str(backup.manifest.get("created_at", "未知时间")),
            "会话数量": len(backup.manifest.get("sessions", [])),
            "目标 provider": str(backup.manifest.get("target_provider", "未知 provider")),
        }
        for backup in backups
    ]
    return resolved_home, resolved_db, rows


def prepare_restore(codex_home: str | Path, state_db: str | Path, backup_dir: str | Path) -> RestorePreview:
    resolved_home = Path(codex_home).expanduser().resolve()
    resolved_db = Path(state_db).expanduser().resolve()
    backup = load_backup_info(Path(backup_dir), resolved_home)
    rows = tuple(
        {
            "Session ID": str(item.get("session_id", "未知 Session ID")),
            "恢复 provider": str(item.get("from_provider", "未知 provider")),
            "会话文件": str(item.get("rollout_path", "未知路径")),
        }
        for item in backup.manifest.get("sessions", [])
        if isinstance(item, dict)
    )
    return RestorePreview(
        resolved_home,
        resolved_db,
        backup.backup_dir,
        backup.backup_dir.name,
        rows,
        str(backup.manifest.get("created_at", "未知时间")),
        str(backup.manifest.get("target_provider", "未知 provider")),
    )


def execute_restore(preview: RestorePreview, confirmation: str) -> ActionResult:
    """重新验证备份后执行恢复，拒绝不匹配确认短语。"""
    if confirmation.strip() != preview.expected_confirmation:
        return ActionResult(False, f"确认短语不匹配，请输入：{preview.expected_confirmation}")
    backup: BackupInfo = load_backup_info(preview.backup_dir, preview.codex_home)
    ensure_codex_not_running()
    rollback_backup = restore_backup(backup, preview.codex_home, preview.state_db)
    return ActionResult(
        True,
        f"恢复完成，JSONL 与 SQLite 已通过校验。恢复前状态备份：{rollback_backup}",
        rollback_backup,
    )
