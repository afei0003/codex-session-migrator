"""文件与 SQLite 的低层安全读写原语。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from .models import ToolError
from .scanner import MAX_META_LINES, open_readonly_sqlite


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    """通过 SQLite Backup API 创建包含 WAL 数据的一致快照。"""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with open_readonly_sqlite(source_path) as source:
        destination = sqlite3.connect(destination_path)
        try:
            source.backup(destination)
        finally:
            destination.close()


def restore_database_snapshot(backup_db: Path, state_db: Path) -> None:
    with open_readonly_sqlite(backup_db) as source:
        destination = sqlite3.connect(state_db)
        try:
            source.backup(destination)
        finally:
            destination.close()


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """在目标同目录落盘后原子替换，避免生成半截 JSONL。"""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def updated_session_json(path: Path, expected_provider: str, target_provider: str) -> bytes:
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
            raise ToolError(f"JSONL provider 已变化，拒绝覆盖：{path}（当前为 {current_provider!r}）")
        payload["model_provider"] = target_provider
        suffix = b"\r\n" if original_line.endswith(b"\r\n") else b"\n"
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        lines[index] = (b"\xef\xbb\xbf" if has_bom else b"") + encoded + suffix
        return b"".join(lines)
    raise ToolError(f"无法更新 JSONL：前 {MAX_META_LINES} 行内未找到 session_meta：{path}")
