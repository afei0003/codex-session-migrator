# Codex Session Provider Migrator

一个用于扫描、迁移和恢复 Codex 本地会话 `model_provider` 的安全命令行工具。

> 当前版本已支持只读扫描和会话选择；迁移写入与恢复能力将在后续提交中实现。

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

## 扫描与选择

```powershell
python codex_session_migrator.py --help
python codex_session_migrator.py --version
python codex_session_migrator.py list --from 2026-07-01 --to 2026-07-31
python codex_session_migrator.py list --provider OpenAI
python codex_session_migrator.py migrate
python codex_session_migrator.py migrate --session-id <SESSION_ID>
```

`list` 只读列出会话；`migrate` 目前只负责按日期和编号选择会话并输出预览，绝不会修改 JSONL 或 SQLite。

扫描会优先使用 `~/.codex/state_5.sqlite`，仅在它不存在时兼容旧位置 `~/.codex/sqlite/state_5.sqlite`。可用 `--codex-home` 或 `--state-db` 明确指定位置。

只有 JSONL 和数据库中的 provider 都存在且完全一致的会话，才可以进入选择列表。解析失败、旧格式缺字段、数据库缺失或两端 provider 不一致的会话会被报告并跳过。

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
