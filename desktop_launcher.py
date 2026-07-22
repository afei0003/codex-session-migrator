#!/usr/bin/env python3
"""Codex Session Migrator 的桌面启动器。

普通模式负责启动隐藏的 Gradio 子进程、打开浏览器和运行系统托盘；
``--server`` 模式是打包程序内部使用的服务入口，不面向最终用户。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Sequence
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 30.0
PROGRAM_NAME = "Codex Session Migrator"


class _NullStream:
    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


class LauncherError(RuntimeError):
    """桌面启动器可以向用户展示的错误。"""


def choose_port(requested_port: int = 0) -> int:
    """选择一个只绑定本机的可用 TCP 端口。"""
    if not 0 <= requested_port <= 65535:
        raise LauncherError("端口必须在 0 到 65535 之间")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, requested_port))
        except OSError as error:
            if requested_port:
                raise LauncherError(f"端口 {requested_port} 已被占用或无法使用") from error
            raise LauncherError("无法选择本机可用端口") from error
        return int(probe.getsockname()[1])


def server_command(port: int) -> list[str]:
    """生成当前程序的内部服务命令，兼容源码和 PyInstaller 模式。"""
    if getattr(sys, "frozen", False):
        executable = [sys.executable]
    else:
        executable = [sys.executable, str(Path(__file__).resolve())]
    return [*executable, "--server", "--port", str(port)]


def _hidden_process_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if getattr(sys, "frozen", False):
        child_environment = os.environ.copy()
        child_environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        options["env"] = child_environment
    return options


@dataclass
class ServerHandle:
    process: subprocess.Popen[bytes]
    url: str

    def stop(self) -> None:
        """停止服务进程；重复调用是安全的。"""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def wait_for_server(
    url: str,
    process: subprocess.Popen[bytes] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    interval: float = 0.2,
) -> None:
    """等待本地 Gradio 服务响应，或在子进程提前退出时立即失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise LauncherError("Gradio 服务启动失败，请检查项目依赖或日志")
        try:
            with urlopen(url, timeout=min(interval, 1.0)) as response:
                if 200 <= response.status < 500:
                    return
        except (OSError, URLError):
            time.sleep(interval)
    raise LauncherError(f"Gradio 服务在 {timeout:.0f} 秒内没有启动完成")


def start_server(requested_port: int = 0, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ServerHandle:
    """启动隐藏的 Gradio 子进程并等待其就绪。"""
    port = choose_port(requested_port)
    url = f"http://{HOST}:{port}"
    process = subprocess.Popen(server_command(port), **_hidden_process_options())
    handle = ServerHandle(process=process, url=url)
    try:
        wait_for_server(url, process=process, timeout=timeout)
    except Exception:
        handle.stop()
        raise
    return handle


def open_browser(url: str) -> bool:
    return bool(webbrowser.open_new(url))


def _create_tray_image() -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (35, 99, 235, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 10, 54, 54), radius=8, fill=(255, 255, 255, 255))
    draw.rectangle((20, 25, 44, 31), fill=(35, 99, 235, 255))
    draw.rectangle((20, 36, 38, 42), fill=(35, 99, 235, 255))
    return image


def run_tray(handle: ServerHandle) -> None:
    """运行托盘循环，直到用户选择退出。"""
    try:
        import pystray
    except ImportError as error:
        raise LauncherError("缺少桌面托盘依赖，请重新安装构建依赖") from error

    def open_interface(_icon: Any, _item: Any) -> None:
        open_browser(handle.url)

    def exit_application(icon: Any, _item: Any) -> None:
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_interface),
        pystray.MenuItem("退出", exit_application),
    )
    icon = pystray.Icon(PROGRAM_NAME, _create_tray_image(), PROGRAM_NAME, menu)
    icon.run()


def show_error(message: str) -> None:
    """在无控制台打包模式下显示错误，否则保留终端输出能力。"""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, PROGRAM_NAME, 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def _log_server_event(message: str) -> None:
    """记录打包服务启动信息到系统临时目录，便于无控制台诊断。"""
    log_path = Path(tempfile.gettempdir()) / "CodexSessionMigrator-server.log"
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 Codex Session Migrator 桌面版")
    parser.add_argument("--port", type=int, default=0, help="本机端口，默认自动选择")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run_server(port: int) -> int:
    if not 1 <= port <= 65535:
        raise LauncherError("内部服务端口必须在 1 到 65535 之间")
    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]
    _log_server_event(f"starting server on port {port}")
    try:
        import gradio_app

        result = gradio_app.main(["--port", str(port), "--no-browser"])
        _log_server_event(f"server exited with code {result}")
        return result
    except BaseException:
        _log_server_event(traceback.format_exc())
        raise


def run_desktop(requested_port: int, no_browser: bool) -> int:
    handle: ServerHandle | None = None
    try:
        handle = start_server(requested_port)
        if not no_browser:
            open_browser(handle.url)
        run_tray(handle)
        return 0
    except (LauncherError, OSError) as error:
        show_error(str(error))
        return 1
    finally:
        if handle is not None:
            handle.stop()


def main(argv: Sequence[str] | None = None) -> int:
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    if args.server:
        try:
            return _run_server(args.port)
        except (LauncherError, OSError) as error:
            show_error(str(error))
            return 1
    return run_desktop(args.port, args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
