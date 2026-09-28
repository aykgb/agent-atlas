# local.json：本机启动参数落盘

来源：2026-09-28 用户需求「支持将 local 的启动参数放到 local.json 下，包括 port 和 --home」，续「启动服务后将所有参数同步到 local.json」。本机与远端配置分家：远端在 `remotes.json`，而本机启动参数此前只能每次敲命令行（如 `./server.sh start --port 18764`），多仓库/多实例并存时易忘、易撞端口（默认 18763 常被另一实例占用）。合并 main（任务 24 跨平台索引隔离）时统一了索引命名口径。

## 现状

- `stats_server.py main()` 的 `--port / --host / --home / --allow-host` 全为内置默认，无配置文件。
- `server.sh`（`parse_url`）与 `server.ps1`（`Get-ServerUrl`）从运行中进程的命令行推导访问地址，因此端口必须出现在命令行里。
- `remotes.json`、`words-exclude.txt` 已是仓库根目录的个人配置（gitignore，不入库）。

## 方案

新增仓库根目录 `local.json`（与 `remotes.json` 对应，不入库）保存本机启动参数：

```json
{ "port": 18764, "host": "127.0.0.1", "home": "/home/wangc", "allow_host": ["stats.example.com"] }
```

- **读取方**：`stats_server.py` 启动时读取，作为 argparse 默认值；文件不存在、坏 JSON 或字段非法则用内置默认，不报错。
- **优先级**：命令行显式参数 > `local.json` > 内置默认。`--allow-host` 给定时整体覆盖（不与配置合并）。
- **字段**：`port`（1..65535 整数）、`host`、`home`（非空字符串）、`allow_host`（字符串或字符串数组，合法但为空时保留空数组）；其它键忽略。
- **启动后写回**：服务启动成功（已监听端口）后，把生效的 `port`、`host`、`home`、`allow_host` 原子写回 `local.json`（权限 600），供下次启动复用；写失败只打印提示，不影响服务。`home` 未显式指定时写为当前用户主目录的绝对路径。`--reindex` 不写回（不监听）。
- **索引命名**：索引文件按解析后的 `home`（`home` 未指定时为当前用户主目录）取 8 位哈希，形如 `index-<哈希>.sqlite`，不同日志根目录各自独立、互不覆盖（`index_suffix`）。与任务 24（跨平台索引隔离）统一口径：不再使用固定的 `index.sqlite`，改为一律按 `home` 命名，共用工作区里 Windows 与 Linux 主目录不同即自动分文件。`--reindex` 同一口径。
- **地址落盘**：服务解析参数后、建索引前，把实际监听地址写入 `.stats/server.url`；`server.sh` / `server.ps1` 优先读该文件推导地址（避免端口只写在 local.json、命令行里没有时脚本探错端口），缺失时回退到既有的命令行解析。
- `.gitignore` 增加 `local.json`（`remotes.json` 已在内）。

## 验收

1. 无 `local.json`：行为与改前一致（端口 18763、`Path.home()`、默认放行名单、`--allow-host` 默认空）。
2. `local.json` 设 `port: 18764` 后 `./server.sh start`（不带参数）：服务起在 18764，脚本打印并探测的是 18764，不再误探 18763。
3. 命令行 `--port 18765` 覆盖 `local.json` 的 18764；`--home`、`--host`、`--allow-host` 同理。
4. 索引一律为 `index-<8位哈希>.sqlite`：`home` 缺省时按当前用户主目录取哈希，显式 `--home`（含来自 `local.json`）按该目录取哈希；`--reindex` 同一口径。
5. 非法 `local.json`（坏 JSON、越界端口、错误类型）不报错，静默回退默认。
6. `local.json`、`remotes.json` 均不被 git 跟踪。
7. 启动不带参数：`local.json` 被写回四键齐全（`port`/`host`/`home`/`allow_host`），值等于生效参数；再次启动仍用同一索引文件。
8. `./server.sh start --port X` 后 `local.json` 的 `port` 变为 X（命令行落盘为新的默认）。
9. `sh -n server.sh`、`unittest`、`node --check static/app.js` 通过。

## 实测（2026-09-28，本机 Linux）

- `local.json` 写 `{"port": 18766}` 后 `./server.sh start`（不带参数）：打印并探测 `http://127.0.0.1:18766`，`.stats/server.url` 内容一致，`status` 同一地址，`/api/meta` 正常返回；`stop` 后 `server.url` 已删除。
- `local.json` 端口 18766 + 命令行 `--port 18767`：以 18767 启动（命令行优先），`server.url` 为 18767。
- `local.json` 加 `"allow_host": ["stats.example.com"]`：日志「远端放行 Host」含该域名与默认 `100.64.216.70`。
- `./server.sh start`（`local.json` 仅 `{"port": 18764}`）：启动后 `local.json` 被写回 `{port:18764, host:"127.0.0.1", home:"/home/wangc", allow_host:[]}`；索引为 `index-3e157b1c.sqlite`（`/home/wangc` 的哈希），不再使用固定的 `index.sqlite`。
- `./server.sh start --port 18801`：`local.json.port` 变为 18801，`server.url` 为 18801；`--home /tmp/opencode`：写回 `home:/tmp/opencode` 并用 `index-736a4cae.sqlite`。18765 当时被无关进程占用，故用 18801 验证。
- `sh -n server.sh`、`unittest`（44 项，+5）、`node --check static/app.js` 通过。
- `server.ps1` 本机无 PowerShell 可执行文件，未实测；改动为 `Get-ServerUrl` 增加「先读 `server.url`」分支并新增 `Wait-ServerUrl`，与 `server.sh` 同构，待 Windows 侧验证。

## 非目标

- 不从网页改配置：`local.json` 只由用户手工编辑或服务启动时写回，没有网页入口。
- `stats-today.py` 的默认 `--url` 不随之改变（仍 18763，需要时用 `--url`）。
- 不改远端配置 `remotes.json` 的语义。
