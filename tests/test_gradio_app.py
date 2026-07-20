from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gradio_app


class _FakeApp:
    def __init__(self) -> None:
        self.launch_arguments: dict[str, object] | None = None

    def launch(self, **kwargs: object) -> None:
        self.launch_arguments = kwargs


class GradioAppTests(unittest.TestCase):
    def test_create_app_builds_blocks(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            app = gradio_app.create_app()
            try:
                self.assertEqual(type(app).__name__, "Blocks")
            finally:
                app.close()

    def test_main_binds_to_loopback_without_sharing(self) -> None:
        fake_app = _FakeApp()
        with patch("gradio_app.create_app", return_value=fake_app):
            exit_code = gradio_app.main(["--port", "7861", "--no-browser"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            fake_app.launch_arguments,
            {
                "server_name": "127.0.0.1",
                "server_port": 7861,
                "inbrowser": False,
                "share": False,
            },
        )

    def test_main_rejects_invalid_port(self) -> None:
        with self.assertRaises(SystemExit):
            gradio_app.main(["--port", "0"])

    def test_selected_ids_reads_only_checked_rows(self) -> None:
        rows = [
            [True, "2026-07-20", "selected-session"],
            [False, "2026-07-20", "ignored-session"],
            [True, "2026-07-20", "selected-session"],
        ]
        self.assertEqual(gradio_app._selected_ids(rows), ["selected-session"])


if __name__ == "__main__":
    unittest.main()
