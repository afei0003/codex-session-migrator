from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_session_migrator as migrator


class ScanSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary_directory.name) / ".codex"
        self.codex_home.mkdir()
        self.state_db = self.codex_home / "state_5.sqlite"
        self._create_database(self.state_db)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _create_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, title TEXT, archived INTEGER)"
        )
        connection.commit()
        connection.close()

    def _insert_thread(self, session_id: str, provider: str, title: str, archived: int = 0) -> None:
        connection = sqlite3.connect(self.state_db)
        connection.execute(
            "INSERT INTO threads (id, model_provider, title, archived) VALUES (?, ?, ?, ?)",
            (session_id, provider, title, archived),
        )
        connection.commit()
        connection.close()

    def _write_session(
        self,
        session_id: str,
        provider: str | None,
        session_date: str = "2026-07-20",
    ) -> Path:
        year, month, day = session_date.split("-")
        path = self.codex_home / "sessions" / year / month / day / f"rollout-{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"id": session_id, "timestamp": f"{session_date}T08:00:00Z"}
        if provider is not None:
            payload["model_provider"] = provider
        path.write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8")
        return path

    def test_scan_merges_title_and_marks_invalid_records(self) -> None:
        valid_id = "valid-session"
        missing_provider_id = "old-session"
        self._insert_thread(valid_id, "custom", "数据库标题")
        self._insert_thread(missing_provider_id, "custom", "旧会话")
        self._write_session(valid_id, "custom")
        self._write_session(missing_provider_id, None)
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": valid_id, "thread_name": "索引标题"}) + "\n",
            encoding="utf-8",
        )

        result = migrator.scan_sessions(self.codex_home)

        records = {record.session_id: record for record in result.sessions}
        self.assertEqual(records[valid_id].title, "索引标题")
        self.assertTrue(records[valid_id].selectable)
        self.assertEqual(records[missing_provider_id].status, "JSONL 缺少 provider")
        self.assertFalse(records[missing_provider_id].selectable)
        self.assertEqual(len(result.issues), 1)

    def test_filter_excludes_archived_and_filters_provider_and_date(self) -> None:
        first_id = "first-session"
        second_id = "second-session"
        self._insert_thread(first_id, "OpenAI", "第一条")
        self._insert_thread(second_id, "custom", "第二条", archived=1)
        self._write_session(first_id, "OpenAI", "2026-07-19")
        self._write_session(second_id, "custom", "2026-07-20")

        result = migrator.scan_sessions(self.codex_home)
        filtered = migrator.filter_sessions(
            result.sessions,
            date_from=date(2026, 7, 19),
            date_to=date(2026, 7, 20),
            provider="OpenAI",
        )
        self.assertEqual([record.session_id for record in filtered], [first_id])
        self.assertEqual(
            [record.session_id for record in migrator.filter_sessions(result.sessions)],
            [first_id],
        )

    def test_malformed_rollout_is_reported_without_stopping_scan(self) -> None:
        session_id = "good-session"
        self._insert_thread(session_id, "custom", "可用会话")
        self._write_session(session_id, "custom")
        malformed = self.codex_home / "sessions" / "2026" / "07" / "20" / "rollout-bad.jsonl"
        malformed.write_text("not json\n", encoding="utf-8")

        result = migrator.scan_sessions(self.codex_home)

        self.assertEqual([record.session_id for record in result.sessions], [session_id])
        self.assertEqual(result.issues[0].code, "无法解析")

    def test_parse_number_selection_rejects_invalid_values(self) -> None:
        self.assertEqual(migrator.parse_number_selection("1,3-4,3", 4), [1, 3, 4])
        with self.assertRaises(migrator.ToolError):
            migrator.parse_number_selection("all", 4)
        with self.assertRaises(migrator.ToolError):
            migrator.parse_number_selection("0", 4)

    def test_root_database_is_preferred_over_legacy_copy(self) -> None:
        legacy = self.codex_home / "sqlite" / "state_5.sqlite"
        legacy.parent.mkdir()
        self._create_database(legacy)
        self.assertEqual(migrator.locate_state_db(self.codex_home), self.state_db.resolve())


if __name__ == "__main__":
    unittest.main()
