# Codex Session Provider Migrator

一个用于扫描、迁移和恢复 Codex 本地会话 `model_provider` 的安全命令行工具。

> 当前仓库处于项目初始化阶段，扫描、迁移和恢复能力将在后续提交中逐步实现。

## 设计目标

- 按日期查看 `~/.codex/sessions/` 中的会话。
- 同时核对 JSONL 会话记录与 `state_5.sqlite` 数据库。
- 写入前展示逐条变更并要求明确确认。
- 每次迁移前创建可验证、可恢复的完整备份。
- 检测到 Codex 正在运行时拒绝修改用户数据。
- 仅使用 Python 标准库，支持 Windows、macOS 和 Linux。

## 环境要求

- Python 3.11 或更高版本
- Codex 本地数据目录，默认位置为 `~/.codex`

## 当前命令骨架

```powershell
python codex_session_migrator.py --help
python codex_session_migrator.py --version
python codex_session_migrator.py list
python codex_session_migrator.py migrate
python codex_session_migrator.py restore
```

现阶段子命令只保留接口，尚不会读取或修改真实 Codex 数据。

## 安全原则

- 默认只读扫描，异常会话只报告、不自动修复。
- 不提供一键迁移全部会话。
- 迁移和恢复前必须关闭 Codex。
- 写入前必须备份并确认，写入后必须验证 JSONL 与 SQLite 一致。

## 开发检查

```powershell
python -m py_compile codex_session_migrator.py
python codex_session_migrator.py --help
python codex_session_migrator.py --version
```
