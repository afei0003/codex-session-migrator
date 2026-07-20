from __future__ import annotations

import json
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_list_output_includes_working_directory(self) -> None:
        session_id = "working-directory-session"
        working_directory = r"D:\workspace\example-project"
        connection = sqlite3.connect(self.state_db)
        connection.execute("ALTER TABLE threads ADD COLUMN cwd TEXT")
        connection.execute(
            "INSERT INTO threads (id, model_provider, title, archived, cwd) VALUES (?, ?, ?, ?, ?)",
            (session_id, "openai", "带工作目录的会话", 0, working_directory),
        )
        connection.commit()
        connection.close()
        self._write_session(session_id, "openai")

        result = migrator.scan_sessions(self.codex_home)
        session = next(item for item in result.sessions if item.session_id == session_id)
        self.assertEqual(session.working_directory, working_directory)
        output = io.StringIO()
        with redirect_stdout(output):
            migrator.print_sessions([session])
        self.assertIn(f"工作目录：{working_directory}", output.getvalue())
        self.assertEqual(
            migrator.display_working_directory(r"\\?\D:\workspace\example-project"),
            working_directory,
        )

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
        self.assertEqual(
            migrator.filter_sessions(result.sessions, provider="openai"),
            [],
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

    def test_discover_providers_merges_config_and_session_history(self) -> None:
        session_id = "provider-session"
        self._insert_thread(session_id, "history-provider", "历史")
        self._write_session(session_id, "history-provider")
        (self.codex_home / "config.toml").write_text(
            'model_provider = "current-provider"\n[model_providers.extra-provider]\n',
            encoding="utf-8",
        )

        providers = migrator.discover_providers(migrator.scan_sessions(self.codex_home))

        self.assertEqual(
            providers,
            ["current-provider", "extra-provider", "history-provider"],
        )

    def test_migration_updates_jsonl_and_database_and_creates_backup(self) -> None:
        session_id = "migrate-session"
        self._insert_thread(session_id, "old-provider", "待迁移")
        rollout = self._write_session(session_id, "old-provider")
        result = migrator.scan_sessions(self.codex_home)
        session = next(record for record in result.sessions if record.session_id == session_id)

        migration = migrator.migrate_sessions(result, [session], "new-provider")

        self.assertEqual(
            migrator._read_session_meta(rollout)["model_provider"], "new-provider"
        )
        self.assertEqual(
            migrator.load_thread_records(self.state_db)[session_id].model_provider,
            "new-provider",
        )
        manifest = json.loads((migration.backup_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["target_provider"], "new-provider")
        self.assertEqual(manifest["sessions"][0]["from_provider"], "old-provider")

    def test_guarded_database_update_rolls_back_when_provider_changes(self) -> None:
        first_id = "first-migrate"
        second_id = "second-migrate"
        self._insert_thread(first_id, "old-provider", "第一条")
        self._insert_thread(second_id, "old-provider", "第二条")
        first_path = self._write_session(first_id, "old-provider")
        second_path = self._write_session(second_id, "old-provider")
        result = migrator.scan_sessions(self.codex_home)
        records = {record.session_id: record for record in result.sessions}
        connection = sqlite3.connect(self.state_db)
        connection.execute(
            "UPDATE threads SET model_provider = ? WHERE id = ?",
            ("concurrent-provider", second_id),
        )
        connection.commit()
        connection.close()

        with self.assertRaises(migrator.ToolError):
            migrator.migrate_sessions(
                result,
                [records[first_id], records[second_id]],
                "new-provider",
            )

        threads = migrator.load_thread_records(self.state_db)
        self.assertEqual(threads[first_id].model_provider, "old-provider")
        self.assertEqual(threads[second_id].model_provider, "concurrent-provider")
        self.assertEqual(migrator._read_session_meta(first_path)["model_provider"], "old-provider")
        self.assertEqual(migrator._read_session_meta(second_path)["model_provider"], "old-provider")

    def test_running_codex_process_is_detected(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout='"Codex.exe","123","Console","1","1 K"\n')
        with patch("csmigrator.process_guard.subprocess.run", return_value=completed):
            self.assertEqual(migrator.find_running_codex_processes(), ["Codex.exe"])

    def test_process_detection_falls_back_to_powershell(self) -> None:
        denied = SimpleNamespace(returncode=1, stdout="")
        fallback = SimpleNamespace(returncode=0, stdout="codex\npython\n")
        with patch("csmigrator.process_guard.subprocess.run", side_effect=[denied, fallback]):
            self.assertEqual(migrator.find_running_codex_processes(), ["codex"])

    def test_restore_recovers_jsonl_and_database_and_backs_up_current_state(self) -> None:
        session_id = "restore-session"
        self._insert_thread(session_id, "old-provider", "待恢复")
        rollout = self._write_session(session_id, "old-provider")
        original = migrator.scan_sessions(self.codex_home)
        record = next(item for item in original.sessions if item.session_id == session_id)
        migration = migrator.migrate_sessions(original, [record], "new-provider")
        backup = migrator.load_backup_info(migration.backup_dir, self.codex_home)

        current_backup = migrator.restore_backup(backup, self.codex_home, self.state_db)

        self.assertEqual(migrator._read_session_meta(rollout)["model_provider"], "old-provider")
        self.assertEqual(
            migrator.load_thread_records(self.state_db)[session_id].model_provider,
            "old-provider",
        )
        rollback = migrator.load_backup_info(current_backup, self.codex_home)
        self.assertEqual(rollback.manifest["operation"], "pre_restore_backup")

    def test_corrupted_backup_is_rejected_before_restore(self) -> None:
        session_id = "corrupt-backup-session"
        self._insert_thread(session_id, "old-provider", "待校验")
        self._write_session(session_id, "old-provider")
        result = migrator.scan_sessions(self.codex_home)
        record = next(item for item in result.sessions if item.session_id == session_id)
        migration = migrator.migrate_sessions(result, [record], "new-provider")
        database_backup = migration.backup_dir / "state_5.sqlite"
        database_backup.write_bytes(database_backup.read_bytes() + b"corrupted")

        with self.assertRaises(migrator.ToolError):
            migrator.load_backup_info(migration.backup_dir, self.codex_home)

    def test_backup_cannot_be_restored_to_another_codex_home(self) -> None:
        session_id = "home-check-session"
        self._insert_thread(session_id, "old-provider", "目录校验")
        self._write_session(session_id, "old-provider")
        result = migrator.scan_sessions(self.codex_home)
        record = next(item for item in result.sessions if item.session_id == session_id)
        migration = migrator.migrate_sessions(result, [record], "new-provider")
        another_home = self.codex_home.parent / ".other-codex"
        another_home.mkdir()

        with self.assertRaises(migrator.ToolError):
            migrator.load_backup_info(migration.backup_dir, another_home)

    def test_dry_run_does_not_modify_selected_session(self) -> None:
        session_id = "dry-run-session"
        self._insert_thread(session_id, "old-provider", "只读预览")
        rollout = self._write_session(session_id, "old-provider")

        with redirect_stdout(io.StringIO()):
            exit_code = migrator.main(
                [
                    "migrate",
                    "--codex-home",
                    str(self.codex_home),
                    "--session-id",
                    session_id,
                    "--to-provider",
                    "new-provider",
                    "--dry-run",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(migrator._read_session_meta(rollout)["model_provider"], "old-provider")
        self.assertEqual(
            migrator.load_thread_records(self.state_db)[session_id].model_provider,
            "old-provider",
        )

    def test_wrong_confirmation_does_not_modify_selected_session(self) -> None:
        session_id = "confirmation-session"
        self._insert_thread(session_id, "old-provider", "确认保护")
        rollout = self._write_session(session_id, "old-provider")

        with (
            patch("csmigrator.process_guard.find_running_codex_processes", return_value=[]),
            patch("builtins.input", return_value="MIGRATE 99"),
            patch.object(migrator, "ensure_codex_not_running"),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = migrator.main(
                [
                    "migrate",
                    "--codex-home",
                    str(self.codex_home),
                    "--session-id",
                    session_id,
                    "--to-provider",
                    "new-provider",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(migrator._read_session_meta(rollout)["model_provider"], "old-provider")


if __name__ == "__main__":
    unittest.main()
