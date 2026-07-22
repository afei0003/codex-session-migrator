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

- 使用 Windows 一键启动版：Windows x64、已安装并使用过 Codex；无需安装 Python。
- 从源码运行 CLI 或 Web 界面：Python 3.11 或更高版本。
- Codex 本地数据目录，默认位置为 `~/.codex`（Windows 通常对应 `%USERPROFILE%\.codex`）。
- 从源码使用 Web 界面时，需要安装 `requirements.txt` 中的 Gradio 依赖。

## Windows 一键启动版（推荐）

Windows 用户不需要安装 Python，也不需要执行命令。推荐直接从 GitHub Release 下载：

[下载 CodexSessionMigrator-windows-x64.zip](https://github.com/afei0003/codex-session-migrator/releases/download/v0.2.0/CodexSessionMigrator-windows-x64.zip)

### 普通用户使用方法

该压缩包适用于 **Windows x64**。下载后按以下步骤操作：

1. 打开上面的 Release 页面，在 **Assets** 区域点击 `CodexSessionMigrator-windows-x64.zip` 下载。
2. 将 zip 压缩包解压到一个单独的文件夹，例如桌面或 `D:\Apps\CodexSessionMigrator`。
3. 打开解压后的文件夹，双击 `CodexSessionMigrator.exe`。
4. 程序会自动启动本机 Web 服务并打开系统默认浏览器，随后即可使用扫描、迁移、备份和恢复功能。

请解压整个压缩包后再运行，不要只把 exe 文件单独复制出来。也不要下载 Release 页面中的 `Source code (zip)`，那是项目源代码，不是可直接运行的程序。

### 运行和退出

- 程序默认只监听本机地址 `127.0.0.1`，不会创建公网链接，也不会暴露给局域网。
- 浏览器标签页关闭不会自动结束后台服务。
- 使用完毕后，点击 Windows 右下角系统托盘中的 CodexSessionMigrator 图标，选择“退出”，程序会关闭后台服务。
- 如果默认端口被占用，程序会自动选择其他可用端口，通常不需要用户处理。

### 首次运行提示

由于当前 Windows 版本尚未进行代码签名，Windows SmartScreen 可能显示“Windows 已保护你的电脑”或“未知发布者”。确认文件来自本项目的 GitHub Release 后，可点击“更多信息”→“仍要运行”。如果 Windows 阻止了解压或运行，也可以先右键 zip 文件，打开“属性”，勾选“解除锁定”（如果该选项存在），再重新解压。

迁移或恢复前，请先彻底关闭 Codex；程序检测到 Codex 正在运行时会拒绝写入，以保护本地数据。

### 自己构建 Windows 版本

如果需要自行构建，先安装 Python 3.11 或更高版本，并在项目目录执行：

```powershell
# 安装构建依赖
python -m pip install -r requirements-build.txt

# 构建 Windows x64 免安装版本
.\build_windows.ps1
```

构建完成后，`dist/CodexSessionMigrator-windows-x64.zip` 即为可分发压缩包。macOS 和 Linux 需要分别构建对应平台版本，不能直接使用这个 Windows 压缩包。

## 本机 Web 界面（源码运行）

如果你使用 Windows，优先推荐上面的免安装版本。本节主要适用于开发者、需要从源码运行的用户，以及 macOS/Linux 用户。

Web 界面仅监听 `127.0.0.1`，不会创建公开链接，也不会暴露给局域网。它直接调用与 CLI 相同的安全业务层，不会通过网页执行 Shell 命令。

### 首次初始化

克隆项目后，先进入项目目录：

```powershell
git clone https://github.com/afei0003/codex-session-migrator.git
cd codex-session-migrator
```

确认已安装 Python 3.11 或更高版本：

```powershell
python --version
```

推荐在项目目录创建本地虚拟环境`.venv`：

```powershell
python -m venv .venv
```

在 PowerShell 中激活刚创建的环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

若 PowerShell 提示禁止运行 `Activate.ps1`，在当前窗口执行下列命令后再激活即可；该设置仅作用于当前 PowerShell 会话：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

激活成功后，命令提示符前会出现 `(.venv)`。

可以用以下命令确认当前 `python` 已经指向项目虚拟环境：

```powershell
Get-Command python
```

输出路径应包含 `.venv\Scripts\python.exe`。

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

### 启动页面

激活虚拟环境后，直接启动：

```powershell
python gradio_app.py
```

浏览器会自动打开 `http://127.0.0.1:7860`。如需改用端口或不自动打开浏览器：

```powershell
python gradio_app.py --port 7861 --no-browser
```

页面运行期间保持此 PowerShell 窗口打开；按 `Ctrl + C` 可停止页面。

### 后续每次使用

`.venv` 已创建且依赖已安装后，只需重复以下三步：

```powershell
cd codex-session-migrator
.\.venv\Scripts\Activate.ps1
python gradio_app.py
```

如果使用的不是 PowerShell，激活命令不同：Windows CMD 使用 `.\.venv\Scripts\activate.bat`；Git Bash 使用 `source .venv/Scripts/activate`。

页面包含两个标签页：

1. **扫描与迁移**：填写筛选条件，点击“扫描会话”，在表格第一列勾选状态为“可迁移”的会话，选择或输入目标 provider，生成预览后输入页面给出的 `MIGRATE <数量>`，再点击“执行迁移”。
2. **备份恢复**：点击“刷新可恢复备份”，勾选一份备份，生成恢复预览后输入页面给出的 `RESTORE <备份ID>`，再点击“执行恢复”。

扫描、刷新备份和生成预览都不会写入数据。

迁移与恢复前仍必须彻底关闭 Codex；页面检测到 Codex 正在运行时会拒绝写入。

所有结果、错误信息和备份路径都会显示在页面中。

如果你的环境设置了 SOCKS 代理，`requirements.txt` 已通过 `httpx[socks]` 安装所需的 `socksio` 支持

## 完整操作步骤

完整的命令行扫描、预览、迁移、验证和恢复步骤：[查看完整操作步骤](docs/完整操作步骤.md)。

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

## License

本项目采用 MIT License，详见 [LICENSE](LICENSE) 文件。

本项目是一个独立的社区工具，与OpenAI无关联或未获得其认可
