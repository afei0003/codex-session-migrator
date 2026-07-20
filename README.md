# Codex Session Provider Migrator

一个用于扫描、迁移和恢复 Codex 本地会话 `model_provider` 的安全工具，提供命令行和仅本机可访问的 Web 界面。

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
- `web_workflows.py`：供 Web 界面调用的扫描、预览、迁移与恢复编排；
- `gradio_app.py`：仅监听本机地址的 Gradio 启动入口。

原有调用方式保持不变；也可以使用 `python -m csmigrator --help`。

## 设计目标

- 按日期查看 `~/.codex/sessions/` 中的会话。
- 同时核对 JSONL 会话记录与 `state_5.sqlite` 数据库。
- 写入前展示逐条变更并要求明确确认。
- 每次迁移前创建可验证、可恢复的完整备份。
- 检测到 Codex 正在运行时拒绝修改用户数据。
- 核心命令行仅使用 Python 标准库，支持 Windows、macOS 和 Linux。

## 环境要求

- Python 3.11 或更高版本
- Codex 本地数据目录，默认位置为 `~/.codex`
- 使用 Web 界面时，需要安装 `requirements.txt` 中的 Gradio 依赖。

## 本机 Web 界面

Web 界面仅监听 `127.0.0.1`，不会创建公开链接，也不会暴露给局域网。它直接调用与 CLI 相同的安全业务层，不会通过网页执行 Shell 命令。

首次使用时，在项目目录创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动页面：

```powershell
.\.venv\Scripts\python.exe gradio_app.py
```

浏览器会自动打开 `http://127.0.0.1:7860`。如需改用端口或不自动打开浏览器：

```powershell
.\.venv\Scripts\python.exe gradio_app.py --port 7861 --no-browser
```

页面包含两个标签页：

1. **扫描与迁移**：填写筛选条件，点击“扫描会话”，在表格第一列勾选状态为“可迁移”的会话，选择或输入目标 provider，生成预览后输入页面给出的 `MIGRATE <数量>`，再点击“执行迁移”。
2. **备份恢复**：点击“刷新可恢复备份”，勾选一份备份，生成恢复预览后输入页面给出的 `RESTORE <备份ID>`，再点击“执行恢复”。

扫描、刷新备份和生成预览都不会写入数据。迁移与恢复前仍必须彻底关闭 Codex；页面检测到 Codex 正在运行时会拒绝写入。所有结果、错误信息和备份路径都会显示在页面中。

如果你的环境设置了 SOCKS 代理，`requirements.txt` 已通过 `httpx[socks]` 安装所需的 `socksio` 支持；请始终使用项目 `.venv` 中的 Python 启动页面。

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

`list` 只读列出会话；每条记录包含 Session ID、标题、当前 provider、状态和工作目录，便于区分来自不同项目的同名或相近会话。`migrate` 会先按日期和编号选择会话，再选择目标 provider，展示逐条预览。`--to-provider` 可跳过目标 provider 菜单；`--dry-run` 始终只预览，不检查进程也不写入。

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

## 完整操作步骤

### 0. 打开 PowerShell 并进入项目目录

克隆项目
```bash
git clone https://github.com/afei0003/codex-session-migrator.git
```

```powershell
cd your path\codex-session-migrator
python --version
```

需要使用 Python 3.11 或更高版本。查看命令帮助：

```powershell
python codex_session_migrator.py --help
```

工具默认读取 `C:\Users\你的用户名\.codex`，优先使用
`C:\Users\你的用户名\.codex\state_5.sqlite`；仅当该文件不存在时，才兼容旧位置
`C:\Users\你的用户名\.codex\sqlite\state_5.sqlite`。

### 1. 先只读扫描会话

此步骤不改动数据，Codex 运行时也可以执行：

```powershell
python codex_session_migrator.py list
```

每条会话会显示日期、Session ID、当前 provider、状态、标题和工作目录。迁移前重点核对：

- `Session ID`：确认目标会话；
- 标题和工作目录：避免选错相似会话；
- 当前 provider：例如 `OpenAI`、`custom`，名称区分大小写；
- 状态为“`一致`”：JSONL 与 SQLite 中的 provider 相同，可以安全迁移。

以下异常会话只会报告，不会进入迁移选择：`Provider 不一致`、`JSONL 缺少 provider`、`数据库缺失`、`重复 Session ID`。

### 2. 缩小扫描范围

按日期查看：

```powershell
python codex_session_migrator.py list --from 2026-07-01 --to 2026-07-31
```

按当前 provider 精确筛选：

```powershell
python codex_session_migrator.py list --provider OpenAI
```

只查看指定会话：

```powershell
python codex_session_migrator.py list --session-id 你的SessionID
```

查看已归档会话：

```powershell
python codex_session_migrator.py list --include-archived
```

参数可以组合使用：

```powershell
python codex_session_migrator.py list --from 2026-07-01 --provider OpenAI
```

### 3. 先做迁移预览

假设要将一个会话迁移到 `custom`：

```powershell
python codex_session_migrator.py migrate --session-id 你的SessionID --to-provider custom --dry-run
```

`--dry-run` 只显示预览，不检查 Codex 进程，也不会写入 JSONL 或 SQLite。预览会列出每条会话的 Session ID、标题和 `当前 provider -> 目标 provider`。

迁移多条会话时，明确列出每个 ID，不提供一键迁移全部会话的方式：

```powershell
python codex_session_migrator.py migrate `
  --session-id SessionID_A SessionID_B SessionID_C `
  --to-provider custom `
  --dry-run
```

不传 `--session-id` 时会进入交互选择：

```powershell
python codex_session_migrator.py migrate --to-provider custom --dry-run
```

先选择日期编号，例如 `1,3-5`，再选择对应会话编号；不接受 `all` 等全选写法。不传 `--to-provider` 时，工具会列出从配置、JSONL 与数据库中发现的 provider，也可以手工输入目标名称。

### 4. 执行真实迁移

真实迁移前，必须彻底退出 Codex Desktop、Codex CLI 等可能访问 `.codex` 的进程。工具检测到 Codex 仍在运行时会拒绝写入，以避免并发覆盖数据。

确认关闭 Codex 后，执行：

```powershell
cd your path\codex-session-migrator

python codex_session_migrator.py migrate `
  --session-id 你的SessionID `
  --to-provider custom
```

工具会再次显示预览。确认无误后，输入精确确认短语：

```text
MIGRATE 1
```

其中 `1` 是本次实际发生变更的会话数量；例如迁移三条会话时输入 `MIGRATE 3`。确认短语不匹配时，工具会取消操作，不写入任何数据。

迁移成功后，工具会在 `.codex\backups\provider-migrations\` 下创建备份，保存选中 JSONL 的原始副本、通过 SQLite Backup API 创建的完整数据库快照，以及包含哈希和 provider 变更信息的 `manifest.json`。随后更新 JSONL 和 SQLite，并重新读取两端验证一致性。

备份目录类似：

```text
C:\Users\用户名\.codex\backups\provider-migrations\20260720T143000000000+0800
```

请保留备份，直到确认会话在 Codex 中可以正常打开。

### 5. 迁移后验证

重新启动 Codex，检查目标会话是否正常显示且可继续对话。也可以再次扫描：

```powershell
python codex_session_migrator.py list --session-id 你的SessionID
```

预期看到目标 provider，且状态仍为“`一致`”。

### 6. 出现问题时恢复备份

恢复同样会写入 JSONL 和 SQLite，因此也必须先彻底关闭 Codex。

先只读预览某份备份：

```powershell
python codex_session_migrator.py restore `
  --backup "C:\Users\用户名\.codex\backups\provider-migrations\备份目录名" `
  --dry-run
```

确认无误后，移除 `--dry-run`：

```powershell
python codex_session_migrator.py restore `
  --backup "C:\Users\用户名\.codex\backups\provider-migrations\备份目录名"
```

工具会要求输入：

```text
RESTORE 备份目录名
```

例如：

```text
RESTORE 20260720T143000000000+0800
```

恢复前会自动备份当前状态；恢复失败时会尝试自动回退。也可以不指定备份目录，进入交互选择：

```powershell
python codex_session_migrator.py restore
```

### 最推荐的实际操作范例

将一个会话迁移到 `custom`：

```powershell
cd your path\codex-session-migrator

python codex_session_migrator.py list --session-id 你的SessionID

python codex_session_migrator.py migrate `
  --session-id 你的SessionID `
  --to-provider custom `
  --dry-run
```

确认预览后，退出 Codex，再执行：

```powershell
python codex_session_migrator.py migrate `
  --session-id 你的SessionID `
  --to-provider custom
```

按提示输入：

```text
MIGRATE 1
```

不要直接手改 JSONL 或 SQLite；始终先用 `--dry-run` 核对，再关闭 Codex 后执行真实迁移。

## 测试

```powershell
.\.venv\Scripts\python.exe -m py_compile codex_session_migrator.py gradio_app.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

测试使用临时 `.codex` 目录，不读取或写入你的真实会话数据。

## 安全原则

- 默认只读扫描，异常会话只报告、不自动修复。
- 不提供一键迁移全部会话。
- 迁移和恢复前必须关闭 Codex。
- 写入前必须备份并确认，写入后必须验证 JSONL 与 SQLite 一致。
