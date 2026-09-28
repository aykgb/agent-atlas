# Windows 与 Linux 共用工作区时的运行环境

来源：2026-09-27 用户需求「本机能否直接运行」→「保留 Windows，Windows 与 Linux 下均要能正常运行」。

## 现状

- 仓库放在 Windows 盘、由 WSL 挂载时只有一份 `.venv`，而 uv 的 venv 按系统创建：Windows 为 `.venv\Scripts\python.exe`，Linux / macOS 为 `.venv/bin/python`；`pyvenv.cfg` 记录创建时的解释器路径，两者互不通用。
- 本机 `.venv` 是 Windows 版，Linux 下 `server.sh` 写死的 `.venv/bin/python` 不存在，无法启动。
- 整个工作区被转成了 CRLF（HEAD 为 LF，`core.autocrlf=false`）：`server.sh` 首行成 `#!/bin/sh\r`，Linux 下直接报 `bad interpreter: /bin/sh^M`，`sh -n` 也报语法错。这比 venv 更早失败，是真正的第一道拦路石。
- `--home` 决定索引文件名：不传 → `.stats/index.sqlite`；传 → `.stats/index-<hash>.sqlite`。两个系统的默认日志根目录不同却共用 `index.sqlite`，会互相覆盖、各自重建，切换系统后还要等一次刷新才显示当前系统的数据。
- 试过在 `.venv` 内建 `bin -> ../.venv-linux/bin` 符号链接复用原路径：CPython 解析到 `/usr/bin/python3`，找不到 `pyvenv.cfg` 而丢掉 site-packages（jieba 导入失败），不可行。

## 方案

- 保留 Windows 的 `.venv` 原样；Linux 侧用 `UV_PROJECT_ENVIRONMENT=.venv-linux uv sync` 另建 `.venv-linux`。
- `server.sh` 新增 `python_bin`：按序探测 `$ROOT/.venv/bin/python`、`$ROOT/.venv-linux/bin/python`，都没有则打印提示并失败退出。`server.ps1` 不变（仍用 `.venv/Scripts/python.exe`）。
- `server.sh` 转回 LF，并新增 `.gitattributes`（`* text=auto` + `*.sh text eol=lf` + `*.ico binary`）：仓库内存 LF、检出按平台，`server.sh` 任何平台都是 LF，同时工作区的 EOL 差异不再算作改动（CRLF 漂移从 `git status` 消失）。
- README / CLAUDE.md 补两个系统各自的环境名与命令；共用工作区时 Linux 侧用 `--home "$HOME"` 让索引独立（`index-*.sqlite`），不覆盖 Windows 的 `index.sqlite`。

## 验收

1. Linux 下 `./server.sh start` / `status` / `stop` 正常。
2. 无任何 venv 时 `./server.sh start` 打印提示并以失败退出，不静默失败。
3. Windows 侧 `server.ps1` 的路径解析不变。
4. `sh -n server.sh`、unittest、`node --check static/app.js` 通过。

## 实测（2026-09-27，本机 WSL Ubuntu 26.04 / Python 3.14.4 / uv 0.12.19，仓库在 /mnt/d）

- `UV_PROJECT_ENVIRONMENT=.venv-linux uv sync`：建出 `.venv-linux`；jieba 0.42.1 在 Python 3.14 下编译安装，分词正常。
- 38 项契约测试通过；`node --check static/app.js` 通过。
- `sh -n server.sh` 通过；`./server.sh start` 打印「已启动 (PID …)，打开 http://127.0.0.1:18763」，`status` 运行中，`stop` 已停止。
- 无 venv 的副本目录里 `sh server.sh start`：打印「未找到 Python 环境…」并 exit 1。
- LF 化之前 `./server.sh` 直接报 `bad interpreter: /bin/sh^M`；LF 化后 `git diff server.sh` 只剩本次 16 行改动（EOL 噪声消失）。
- 共用工作区的索引隔离：Linux 不传 `--home` 时读到的是 Windows 建好的 `index.sqlite`（1676 轮、含 claude/codex/pi/dsh）；传 `--home "$HOME"` 时得到独立索引 `index-3e157b1c.sqlite`（38 轮、仅 opencode，符合本机实际日志）。
- 未实测 Windows `server.ps1`（本机为 Linux）；改动只涉及 `server.sh`，ps1 未动。

## 非目标

- 不合并 Windows 与 Linux 的 venv：同一目录无法同时满足两套布局与 `pyvenv.cfg`。
- 不逐个转换工作区文件：EOL 交给 `.gitattributes` 的 `* text=auto` 处理（仓库内存 LF、检出按平台），不手工改写其余文件。
- 不在 `server.sh` 里自动注入 `--home`（避免隐式改 CLI 语义），由使用者显式传入。

## 后续（2026-09-28，任务 25）

索引命名已统一：本机索引一律按解析后的日志根目录（`home`，未指定为当前用户主目录）取 8 位哈希，形如 `index-<哈希>.sqlite`，不再使用固定的 `index.sqlite`。因此共用工作区里 Windows 与 Linux 主目录不同即自动落到不同索引文件，本文档「Linux 侧显式 `--home "$HOME"` 以隔离索引」不再是必需（`--home "$HOME"` 仍指向同一文件 `index-3e157b1c.sqlite`）。
