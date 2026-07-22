from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_launcher


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class _FakeResponse:
    status = 200

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class DesktopLauncherTests(unittest.TestCase):
    def test_choose_port_returns_available_port(self) -> None:
        port = desktop_launcher.choose_port()
        self.assertGreater(port, 0)

    def test_choose_port_rejects_invalid_port(self) -> None:
        with self.assertRaises(desktop_launcher.LauncherError):
            desktop_launcher.choose_port(65536)

    def test_server_command_uses_internal_server_mode(self) -> None:
        command = desktop_launcher.server_command(43210)
        self.assertEqual(command[-3:], ["--server", "--port", "43210"])

    def test_wait_for_server_returns_when_local_endpoint_is_ready(self) -> None:
        with patch("desktop_launcher.urlopen", return_value=_FakeResponse()):
            desktop_launcher.wait_for_server("http://127.0.0.1:43210", timeout=0.1)

    def test_wait_for_server_fails_when_process_exits(self) -> None:
        process = _FakeProcess(returncode=1)
        with self.assertRaises(desktop_launcher.LauncherError):
            desktop_launcher.wait_for_server("http://127.0.0.1:43210", process=process, timeout=0.1)

    def test_start_server_cleans_up_when_readiness_fails(self) -> None:
        process = _FakeProcess()
        with patch("desktop_launcher.choose_port", return_value=43210), patch(
            "desktop_launcher.subprocess.Popen", return_value=process
        ), patch("desktop_launcher.wait_for_server", side_effect=desktop_launcher.LauncherError("timeout")):
            with self.assertRaises(desktop_launcher.LauncherError):
                desktop_launcher.start_server(timeout=0.1)
        self.assertTrue(process.terminated)

    def test_server_handle_stop_is_idempotent(self) -> None:
        process = _FakeProcess()
        handle = desktop_launcher.ServerHandle(process, "http://127.0.0.1:43210")
        handle.stop()
        handle.stop()
        self.assertTrue(process.terminated)

    def test_main_server_mode_delegates_to_gradio(self) -> None:
        with patch("desktop_launcher._run_server", return_value=0) as run_server:
            self.assertEqual(desktop_launcher.main(["--server", "--port", "43210"]), 0)
        run_server.assert_called_once_with(43210)


if __name__ == "__main__":
    unittest.main()
