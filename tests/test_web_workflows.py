from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csmigrator import web_workflows
from csmigrator.scanner import load_thread_records, read_session_meta
from csmigrator.models import ToolError


class WebWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary_directory.name) / ".codex"
        self.codex_home.mkdir()
        self.state_db = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(self.state_db)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, title TEXT, cwd TEXT, archived INTEGER)"
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _insert_thread(self, session_id: str, provider: str, title: str, cwd: str = "", archived: int = 0) -> None:
        connection = sqlite3.connect(self.state_db)
        connection.execute(
            "INSERT INTO threads (id, model_provider, title, cwd, archived) VALUES (?, ?, ?, ?, ?)",
            (session_id, provider, title, cwd, archived),
        )
        connection.commit()
        connection.close()

    def _write_session(self, session_id: str, provider: str | None, session_date: str = "2026-07-20") -> Path:
        year, month, day = session_date.split("-")
        path = self.codex_home / "sessions" / year / month / day / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"id": session_id, "timestamp": f"{session_date}T08:00:00Z"}
        if provider is not None:
            payload["model_provider"] = provider
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8")
        return path

    def _scan(self) -> web_workflows.WebScanResult:
        request = web_workflows.build_scan_request(self.codex_home, self.state_db)
        return web_workflows.scan_for_web(request)

    def test_scan_rows_include_filters_and_working_directory(self) -> None:
        self._insert_thread("first", "openai", "第一条", r"D:\workspace\first")
        self._insert_thread("second", "custom", "第二条", r"D:\workspace\second")
        self._write_session("first", "openai", "2026-07-19")
        self._write_session("second", "custom", "2026-07-20")
        request = web_workflows.build_scan_request(
            self.codex_home,
            self.state_db,
            date_from="2026-07-20",
            provider="custom",
        )

        scan = web_workflows.scan_for_web(request)

        self.assertEqual([row["Session ID"] for row in scan.rows], ["second"])
        self.assertEqual(scan.rows[0]["工作目录"], r"D:\workspace\second")
        self.assertTrue(scan.rows[0]["可迁移"])
        self.assertIn("custom", scan.providers)

    def test_scan_rows_truncate_long_titles_and_accept_calendar_timestamp(self) -> None:
        title = "123456789012345678901"
        self._insert_thread("long-title", "openai", title)
        self._write_session("long-title", "openai")
        request = web_workflows.build_scan_request(
            self.codex_home,
            self.state_db,
            date_from="2026-07-20T00:00:00",
        )

        scan = web_workflows.scan_for_web(request)

        self.assertEqual(scan.rows[0]["标题"], "1234567890123456789…")

    def test_preview_rejects_unselectable_session(self) -> None:
        self._insert_thread("missing-provider", "old", "异常会话")
        self._write_session("missing-provider", None)

        with self.assertRaises(ToolError):
            web_workflows.prepare_migration(self._scan(), ["missing-provider"], "new")

    def test_wrong_migration_confirmation_does_not_write(self) -> None:
        self._insert_thread("confirmation", "old", "确认保护")
        rollout = self._write_session("confirmation", "old")
        preview = web_workflows.prepare_migration(self._scan(), ["confirmation"], "new")

        action = web_workflows.execute_migration(preview, "MIGRATE 99")

        self.assertFalse(action.success)
        self.assertEqual(read_session_meta(rollout)["model_provider"], "old")
        self.assertEqual(load_thread_records(self.state_db)["confirmation"].model_provider, "old")

    def test_migration_executes_after_fresh_validation(self) -> None:
        self._insert_thread("migrate", "old", "可迁移会话")
        rollout = self._write_session("migrate", "old")
        preview = web_workflows.prepare_migration(self._scan(), ["migrate"], "new")

        with patch("csmigrator.web_workflows.ensure_codex_not_running"):
            action = web_workflows.execute_migration(preview, preview.expected_confirmation)

        self.assertTrue(action.success)
        self.assertIsNotNone(action.backup_dir)
        self.assertEqual(read_session_meta(rollout)["model_provider"], "new")
        self.assertEqual(load_thread_records(self.state_db)["migrate"].model_provider, "new")

    def test_restore_listing_preview_and_execution(self) -> None:
        self._insert_thread("restore", "old", "待恢复会话")
        rollout = self._write_session("restore", "old")
        migration_preview = web_workflows.prepare_migration(self._scan(), ["restore"], "new")
        with patch("csmigrator.web_workflows.ensure_codex_not_running"):
            migration = web_workflows.execute_migration(
                migration_preview,
                migration_preview.expected_confirmation,
            )
        self.assertIsNotNone(migration.backup_dir)

        home, state_db, backups = web_workflows.list_backups_for_web(self.codex_home, self.state_db)
        self.assertEqual(home, self.codex_home.resolve())
        self.assertEqual(state_db, self.state_db.resolve())
        self.assertEqual(len(backups), 1)
        preview = web_workflows.prepare_restore(home, state_db, backups[0]["备份目录"])

        with patch("csmigrator.web_workflows.ensure_codex_not_running"):
            action = web_workflows.execute_restore(preview, preview.expected_confirmation)

        self.assertTrue(action.success)
        self.assertEqual(read_session_meta(rollout)["model_provider"], "old")
        self.assertEqual(load_thread_records(self.state_db)["restore"].model_provider, "old")

    def test_restore_requires_exact_confirmation(self) -> None:
        self._insert_thread("restore-confirmation", "old", "恢复确认")
        self._write_session("restore-confirmation", "old")
        migration_preview = web_workflows.prepare_migration(self._scan(), ["restore-confirmation"], "new")
        with patch("csmigrator.web_workflows.ensure_codex_not_running"):
            migration = web_workflows.execute_migration(
                migration_preview,
                migration_preview.expected_confirmation,
            )
        preview = web_workflows.prepare_restore(self.codex_home, self.state_db, migration.backup_dir)

        action = web_workflows.execute_restore(preview, "RESTORE invalid")

        self.assertFalse(action.success)
        self.assertEqual(load_thread_records(self.state_db)["restore-confirmation"].model_provider, "new")


if __name__ == "__main__":
    unittest.main()
