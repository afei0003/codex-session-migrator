#!/usr/bin/env python3
"""Codex 会话 provider 迁移工具的本机 Gradio 界面。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
from typing import Any, MutableMapping


def configure_localhost_proxy_bypass(environment: MutableMapping[str, str] | None = None) -> None:
    """确保 Gradio 的本机启动请求不会被用户配置的代理接管。"""
    target = environment if environment is not None else os.environ
    localhost_hosts = ("127.0.0.1", "localhost", "::1")
    for name in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in target.get(name, "").split(",") if item.strip()]
        known = {item.casefold() for item in entries}
        entries.extend(host for host in localhost_hosts if host.casefold() not in known)
        target[name] = ",".join(entries)


# 必须在导入 Gradio 前设置。Gradio 会在导入与启动阶段向 127.0.0.1 发起 HTTP 请求。
configure_localhost_proxy_bypass()

import gradio as gr

from csmigrator.models import ToolError
from csmigrator.scanner import default_codex_home
from csmigrator.web_workflows import (
    MigrationPreview,
    RestorePreview,
    WebScanResult,
    build_scan_request,
    execute_migration,
    execute_restore,
    list_backups_for_web,
    prepare_migration,
    prepare_restore,
    scan_for_web,
)


SESSION_HEADERS = [
    "选择",
    "日期",
    "Session ID",
    "标题",
    "当前 provider",
    "状态",
    "可迁移",
    "已归档",
    "工作目录",
]
MIGRATION_HEADERS = ["Session ID", "标题", "当前 provider", "目标 provider", "工作目录"]
BACKUP_HEADERS = ["选择", "备份 ID", "备份目录", "创建时间", "会话数量", "目标 provider"]
RESTORE_HEADERS = ["Session ID", "恢复 provider", "会话文件"]


def _rows_for_headers(rows: Sequence[dict[str, object]], headers: Sequence[str]) -> list[list[object]]:
    return [[row.get(header, "") for header in headers] for row in rows]


def _selected_ids(table_rows: Any, identifier_column: int = 2) -> list[str]:
    """从可编辑表格的首列复选框中读取勾选记录。"""
    if hasattr(table_rows, "tolist"):
        table_rows = table_rows.tolist()
    if not isinstance(table_rows, list):
        return []
    selected: list[str] = []
    for row in table_rows:
        if not isinstance(row, (list, tuple)) or len(row) <= identifier_column:
            continue
        if bool(row[0]) and isinstance(row[identifier_column], str) and row[identifier_column]:
            selected.append(row[identifier_column])
    return list(dict.fromkeys(selected))


def _message(prefix: str, value: str) -> str:
    return f"### {prefix}\n\n{value}"


def _scan_callback(
    codex_home: str,
    state_db: str,
    date_from: str,
    date_to: str,
    provider: str,
    session_ids: str,
    include_archived: bool,
) -> tuple[Any, WebScanResult | None, Any, str, list[list[object]], MigrationPreview | None, str]:
    try:
        request = build_scan_request(
            codex_home,
            state_db,
            date_from,
            date_to,
            provider,
            session_ids,
            include_archived,
        )
        web_scan = scan_for_web(request)
        issue_text = "\n".join(f"- {item}" for item in web_scan.issues) or "无异常会话。"
        message = _message(
            "扫描完成",
            f"扫描到 {len(web_scan.result.sessions)} 条会话，筛选后 {len(web_scan.sessions)} 条。\n\n异常报告：\n{issue_text}",
        )
        return (
            _rows_for_headers(web_scan.rows, SESSION_HEADERS),
            web_scan,
            gr.Dropdown(choices=web_scan.providers, value=None, allow_custom_value=True),
            message,
            [],
            None,
            "",
        )
    except (ToolError, OSError) as error:
        return [], None, gr.Dropdown(choices=[], value=None, allow_custom_value=True), _message("扫描失败", str(error)), [], None, ""


def _migration_preview_callback(
    web_scan: WebScanResult | None,
    session_table: Any,
    target_provider: str,
) -> tuple[list[list[object]], MigrationPreview | None, str, str]:
    try:
        if web_scan is None:
            raise ToolError("请先扫描会话")
        preview = prepare_migration(web_scan, _selected_ids(session_table), target_provider)
        return (
            _rows_for_headers(preview.rows, MIGRATION_HEADERS),
            preview,
            _message("迁移预览已生成", f"请核对下表；执行时必须输入：`{preview.expected_confirmation}`"),
            preview.expected_confirmation,
        )
    except ToolError as error:
        return [], None, _message("无法生成迁移预览", str(error)), ""


def _migration_execute_callback(
    preview: MigrationPreview | None,
    confirmation: str,
    session_table: Any,
    current_scan: WebScanResult | None,
) -> tuple[str, Any, WebScanResult | None, Any, list[list[object]], MigrationPreview | None, str]:
    try:
        if preview is None:
            raise ToolError("请先生成迁移预览")
        action = execute_migration(preview, confirmation)
        if not action.success:
            return _message("迁移未执行", action.message), session_table, current_scan, gr.skip(), _rows_for_headers(preview.rows, MIGRATION_HEADERS), preview, preview.expected_confirmation
        refreshed = scan_for_web(preview.request)
        return (
            _message("迁移完成", action.message),
            _rows_for_headers(refreshed.rows, SESSION_HEADERS),
            refreshed,
            gr.Dropdown(choices=refreshed.providers, value=None, allow_custom_value=True),
            [],
            None,
            "",
        )
    except (ToolError, OSError) as error:
        return _message("迁移失败", str(error)), session_table, current_scan, gr.skip(), [], None, ""


def _backup_refresh_callback(codex_home: str, state_db: str) -> tuple[list[list[object]], str, str, str]:
    try:
        resolved_home, resolved_db, backups = list_backups_for_web(codex_home, state_db)
        rows = [[False, *row] for row in _rows_for_headers(backups, BACKUP_HEADERS[1:])]
        return rows, str(resolved_home), str(resolved_db), _message("备份列表已刷新", f"发现 {len(backups)} 份可恢复备份。")
    except (ToolError, OSError) as error:
        return [], codex_home, state_db, _message("无法读取备份", str(error))


def _restore_preview_callback(codex_home: str, state_db: str, backup_table: Any) -> tuple[list[list[object]], RestorePreview | None, str, str]:
    try:
        selected = _selected_ids(backup_table, identifier_column=2)
        if len(selected) != 1:
            raise ToolError("请勾选且仅勾选一份备份")
        preview = prepare_restore(codex_home, state_db, selected[0])
        return (
            _rows_for_headers(preview.rows, RESTORE_HEADERS),
            preview,
            _message("恢复预览已生成", f"备份：`{preview.backup_id}`；执行时必须输入：`{preview.expected_confirmation}`"),
            preview.expected_confirmation,
        )
    except (ToolError, OSError) as error:
        return [], None, _message("无法生成恢复预览", str(error)), ""


def _restore_execute_callback(
    preview: RestorePreview | None,
    confirmation: str,
    codex_home: str,
    state_db: str,
    backup_table: Any,
) -> tuple[str, list[list[object]], str, str, list[list[object]], RestorePreview | None, str]:
    try:
        if preview is None:
            raise ToolError("请先生成恢复预览")
        action = execute_restore(preview, confirmation)
        if not action.success:
            return _message("恢复未执行", action.message), backup_table, codex_home, state_db, _rows_for_headers(preview.rows, RESTORE_HEADERS), preview, preview.expected_confirmation
        resolved_home, resolved_db, backups = list_backups_for_web(codex_home, state_db)
        rows = [[False, *row] for row in _rows_for_headers(backups, BACKUP_HEADERS[1:])]
        return _message("恢复完成", action.message), rows, str(resolved_home), str(resolved_db), [], None, ""
    except (ToolError, OSError) as error:
        return _message("恢复失败", str(error)), backup_table, codex_home, state_db, [], None, ""


def create_app(default_home: Path | None = None, default_state_db: Path | None = None) -> gr.Blocks:
    """创建界面而不启动服务器，便于测试。"""
    home_value = str((default_home or default_codex_home()).expanduser())
    state_value = str(default_state_db.expanduser()) if default_state_db else ""
    with gr.Blocks(title="Codex 会话 Provider 迁移工具") as app:
        gr.Markdown(
            "# Codex 会话 Provider 迁移工具\n\n"
            "仅限本机访问。扫描和预览不写入数据；迁移或恢复前必须彻底关闭 Codex。"
        )
        with gr.Row():
            codex_home = gr.Textbox(label="Codex 数据目录", value=home_value)
            state_db = gr.Textbox(label="SQLite 数据库路径（可选）", value=state_value)

        with gr.Tab("扫描与迁移"):
            with gr.Row():
                date_from = gr.DateTime(
                    label="起始日期",
                    include_time=False,
                    type="string",
                )
                date_to = gr.DateTime(
                    label="结束日期",
                    include_time=False,
                    type="string",
                )
                provider_filter = gr.Textbox(label="当前 provider 筛选（区分大小写）")
            session_ids = gr.Textbox(label="Session ID 筛选（多个 ID 用空格、逗号或换行分隔）", lines=2)
            include_archived = gr.Checkbox(label="包含已归档会话", value=False)
            scan_button = gr.Button("扫描会话", variant="secondary")
            scan_status = gr.Markdown()
            scan_state = gr.State(value=None)
            session_table = gr.Dataframe(
                headers=SESSION_HEADERS,
                datatype=["bool", "str", "str", "str", "str", "str", "bool", "bool", "str"],
                type="array",
                interactive=True,
                label="会话列表（仅勾选第一列）",
                column_widths=[60, 100, 300, 180, 140, 120, 90, 90, 260],
            )
            with gr.Row():
                target_provider = gr.Dropdown(label="目标 provider", choices=[], allow_custom_value=True)
                preview_button = gr.Button("生成迁移预览")
            migration_status = gr.Markdown()
            migration_table = gr.Dataframe(headers=MIGRATION_HEADERS, type="array", interactive=False, label="迁移预览")
            migration_state = gr.State(value=None)
            migration_confirmation = gr.Textbox(label="确认短语", placeholder="先生成预览")
            migration_execute_button = gr.Button("执行迁移", variant="stop")

            scan_button.click(
                _scan_callback,
                [codex_home, state_db, date_from, date_to, provider_filter, session_ids, include_archived],
                [session_table, scan_state, target_provider, scan_status, migration_table, migration_state, migration_confirmation],
                api_name=False,
            )
            preview_button.click(
                _migration_preview_callback,
                [scan_state, session_table, target_provider],
                [migration_table, migration_state, migration_status, migration_confirmation],
                api_name=False,
            )
            migration_execute_button.click(
                _migration_execute_callback,
                [migration_state, migration_confirmation, session_table, scan_state],
                [migration_status, session_table, scan_state, target_provider, migration_table, migration_state, migration_confirmation],
                api_name=False,
            )

        with gr.Tab("备份恢复"):
            gr.Markdown("恢复同样会写入 JSONL 与 SQLite；请先刷新列表、勾选一份备份并生成预览。")
            refresh_backups_button = gr.Button("刷新可恢复备份", variant="secondary")
            backup_status = gr.Markdown()
            backup_table = gr.Dataframe(
                headers=BACKUP_HEADERS,
                datatype=["bool", "str", "str", "str", "number", "str"],
                type="array",
                interactive=True,
                label="可恢复备份（仅勾选第一列）",
            )
            restore_preview_button = gr.Button("生成恢复预览")
            restore_status = gr.Markdown()
            restore_table = gr.Dataframe(headers=RESTORE_HEADERS, type="array", interactive=False, label="恢复范围")
            restore_state = gr.State(value=None)
            restore_confirmation = gr.Textbox(label="确认短语", placeholder="先生成恢复预览")
            restore_execute_button = gr.Button("执行恢复", variant="stop")

            refresh_backups_button.click(
                _backup_refresh_callback,
                [codex_home, state_db],
                [backup_table, codex_home, state_db, backup_status],
                api_name=False,
            )
            restore_preview_button.click(
                _restore_preview_callback,
                [codex_home, state_db, backup_table],
                [restore_table, restore_state, restore_status, restore_confirmation],
                api_name=False,
            )
            restore_execute_button.click(
                _restore_execute_callback,
                [restore_state, restore_confirmation, codex_home, state_db, backup_table],
                [restore_status, backup_table, codex_home, state_db, restore_table, restore_state, restore_confirmation],
                api_name=False,
            )
    return app.queue(default_concurrency_limit=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 Codex 会话 Provider 迁移工具的本机 Web 界面。")
    parser.add_argument("--codex-home", type=Path, help="预填的 Codex 数据目录，默认 ~/.codex")
    parser.add_argument("--state-db", type=Path, help="预填的 state_5.sqlite 路径")
    parser.add_argument("--port", type=int, default=7860, help="本机端口，默认 7860")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser


def launch_app(app: gr.Blocks, port: int, *, inbrowser: bool) -> None:
    """在本机启动 Gradio 服务。

    桌面启动器会复用这个函数，并自行负责浏览器和后台进程生命周期。
    """
    app.launch(
        server_name="127.0.0.1",
        server_port=port,
        inbrowser=inbrowser,
        share=False,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("错误：--port 必须在 1 到 65535 之间")
    app = create_app(args.codex_home, args.state_db)
    launch_app(app, args.port, inbrowser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
