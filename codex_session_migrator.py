#!/usr/bin/env python3
"""Codex Session Provider 迁移工具。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


PROGRAM_NAME = "codex-session-migrator"
VERSION = "0.1.0"


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="安全扫描、迁移并恢复 Codex 会话的 model_provider。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.add_parser("list", help="扫描并列出 Codex 会话")
    subparsers.add_parser("migrate", help="预览并迁移会话 provider")
    subparsers.add_parser("restore", help="从迁移备份恢复会话")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行命令行入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    print(f"“{args.command}”功能将在后续实施阶段完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
