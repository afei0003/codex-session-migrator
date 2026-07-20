# Codex Session Provider Migrator

一个用于扫描、迁移和恢复 Codex 本地会话 `model_provider` 的安全命令行工具。

> 当前版本已支持扫描、选择、迁移、备份和恢复。

## 代码结构

项目保留 `codex_session_migrator.py` 作为兼容入口，业务实现按职责拆分在 `csmigrator/`：

- `scanner.py`：扫描 JSONL、SQLite 与筛选会话；
- `selection.py`：交互选择、provider 发现与终端预览；
- `storage.py`：原子文件写入、SQLite 快照和哈希；
- `migration.py`：迁移、备份、回滚与写后校验；
- `restore.py`：备份校验与恢复；
- `process_guard.py`：写入前的 Codex 进程保护；
- `cli.py`：命令行参数和流程编排。

原有调用方式保持不变；也可以使用 `python -m csmigrator --help`。

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
python codex_session_migrator.py migrate --session-id <SESSION_ID> --to-provider custom --dry-run
python codex_session_migrator.py restore
python codex_session_migrator.py restore --backup <备份目录> --dry-run
```

`list` 只读列出会话。`migrate` 会先按日期和编号选择会话，再选择目标 provider，展示逐条预览。`--to-provider` 可跳过目标 provider 菜单；`--dry-run` 始终只预览，不检查进程也不写入。

扫描会优先使用 `~/.codex/state_5.sqlite`，仅在它不存在时兼容旧位置 `~/.codex/sqlite/state_5.sqlite`。可用 `--codex-home` 或 `--state-db` 明确指定位置。

只有 JSONL 和数据库中的 provider 都存在且完全一致的会话，才可以进入选择列表。解析失败、旧格式缺字段、数据库缺失或两端 provider 不一致的会话会被报告并跳过。

`model_provider` 名称区分大小写，例如 `OpenAI` 与 `openai` 是两个不同分组。目标 provider 可以从检测列表选择，也可以手工输入；手工输入时请使用配置中的精确名称。

## 迁移写入流程

真实迁移前，请彻底关闭 Codex。工具会检测 `Codex` 进程；检测到运行时将拒绝写入。

确认目标 provider 后，工具会展示每条会话的 `当前 provider -> 目标 provider`。只有输入精确短语 `MIGRATE <数量>` 才会继续。

写入前，工具会在 `~/.codex/backups/provider-migrations/<时间戳>/` 创建：

- 选中 JSONL 的原始副本；
- 通过 SQLite Backup API 生成的 `state_5.sqlite` 一致快照；
- 包含哈希、源路径、旧 provider 和目标 provider 的 `manifest.json`。

随后工具在 SQLite 事务中按 Session ID 和旧 provider 更新 `threads` 表，并原子替换对应 JSONL 的 `session_meta.model_provider`。任一受控步骤失败时，会回滚数据库并恢复已经替换的 JSONL；成功后会重新读取 JSONL 和 SQLite 验证一致性。

## 恢复备份

`restore` 默认列出 `~/.codex/backups/provider-migrations/` 中哈希校验通过的备份，也可以用 `--backup` 指定目录。恢复只允许作用于备份记录的同一份 Codex 数据目录，且会检查每个备份 JSONL 与数据库快照的 SHA-256。

恢复前必须关闭 Codex，并输入 `RESTORE <备份目录名>`。工具会先创建“恢复前状态”备份；若恢复过程失败，会自动尝试回退到该状态。恢复完成后会校验 JSONL 内容哈希，以及每条 Session 的 JSONL 与 SQLite provider 是否一致。

备份不能恢复到另一份 Codex 数据目录：SQLite 快照内包含本机的会话路径，因此工具会校验 `manifest.json` 中的 `codex_home` 并拒绝跨目录写入。

## 推荐操作顺序

1. 先运行 `list`，确认目标 Session ID、标题和当前 provider。
2. 用 `migrate --session-id ... --to-provider ... --dry-run` 检查预览。
3. 彻底关闭 Codex，再移除 `--dry-run` 执行真实迁移。
4. 迁移后重启 Codex 验证；如需撤销，使用 `restore --dry-run` 检查备份，再执行恢复。

## 测试

```powershell
python -m py_compile codex_session_migrator.py
python -m unittest discover -s tests -v
git diff --check
```

测试使用临时 `.codex` 目录，不读取或写入你的真实会话数据。

## 安全原则

- 默认只读扫描，异常会话只报告、不自动修复。
- 不提供一键迁移全部会话。
- 迁移和恢复前必须关闭 Codex。
- 写入前必须备份并确认，写入后必须验证 JSONL 与 SQLite 一致。
