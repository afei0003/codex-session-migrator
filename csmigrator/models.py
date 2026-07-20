"""领域数据模型和可预期错误。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


class ToolError(RuntimeError):
    """向用户展示的可预期错误。"""


@dataclass(frozen=True)
class ThreadRecord:
    """`state_5.sqlite` 中的线程摘要。"""

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
        """只有双端完整且一致的会话可以在迁移阶段选择。"""
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
class ParsedSession:
    """尚未与 SQLite 合并的 JSONL 元数据。"""

    session_id: str
    timestamp: datetime | None
    session_date: date
    model_provider: str | None
    rollout_path: Path
    archived: bool
