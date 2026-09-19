# 控制脚本阻塞到服务就绪（server.sh / server.ps1）

来源：2026-09-19 用户需求「server 未就绪则阻塞」。`restart` 后脚本只探测一次 `/api/meta`，索引仍在构建（HTTP 尚未监听）时打印「尚未就绪」就退出，用户要手动再跑 `status` 确认。

## 现状

- `stats_server.py main()`：首次启动或 schema 升级时 `rebuild()` 在 HTTP 监听之前执行，构建期间 `/api/meta` 连接被拒。
- `server.ps1`：`Start-Server` 启动后 sleep 1s 探测一次 meta，未就绪打印「运行中（索引构建中，尚未就绪）」退出；`Show-ServerStatus` 同样只探测一次，未就绪打印「未响应（索引构建中）」退出。
- `server.sh`：`start` 只 sleep 1s 检查进程存活，不探测就绪；`status` 只查 PID。

## 方案

统一规则：**服务未就绪则阻塞到就绪**（每 1s 探测 `/api/meta`；等待期间服务进程退出则按启动失败处理，打印日志尾部）。不设超时，Ctrl+C 可中断。

- `server.ps1` 新增 `Wait-ServerReady`：循环探测 meta，首次未就绪打印「索引构建中，等待就绪…」；每轮检查进程存活，进程消失则清 PID 文件、打印日志尾部、exit 1。调用点：
  - `Start-Server` 新启动分支：进程确认存活后阻塞到就绪，再打印「状态: 运行中」。
  - `Start-Server`「已在运行」分支：同样阻塞到就绪（索引已建好时首探即成功，无额外等待）。
  - `Show-ServerStatus`：首探未就绪时阻塞到就绪，再打印「响应正常（…）」。
- `server.sh` 新增 `parse_url`（从参数列表解析 `--port`/`--host`，口径同 ps1 与 `stats_server.py` 启动打印）与 `wait_ready`（curl 探测 `/api/meta`，进程消失返回 1）。调用点：
  - `start` 新启动分支：进程确认存活后阻塞到就绪。
  - `start`「已在运行」分支与 `status`：从运行中进程命令行（`ps -p PID -o command=`）解析地址后阻塞到就绪。

## 验收

1. 无本机索引时 `restart`：脚本阻塞并打印「索引构建中，等待就绪…」，构建完成后打印「状态: 运行中」，期间不提前退出。
2. 索引已就绪时 `start` / `status`：首探即成功，无感知等待。
3. 等待期间服务进程被杀：脚本以失败退出并打印日志尾部，不无限阻塞。
4. `--port 18764` 启动：探测的是 18764 而非默认端口。
5. `sh -n server.sh`、PowerShell 语法解析、`unittest`、`node --check static/app.js` 通过。

## 实测（2026-09-19，本机 Windows）

- 移走本机索引后 `restart`：阻塞 8s，打印「索引构建中，等待就绪…」，重建完成后打印「状态: 运行中」返回；索引已就绪时 `start` 1s 内返回、无提示行。
- 就绪时 `status`：226ms 返回「响应正常」。
- 等待期间杀掉服务进程：脚本打印「启动失败，最近日志：」+ 日志尾部（stderr）后 exit 1，PID 文件已清理，不挂死。
- `--port` 未单独实测：URL 解析（`Get-ServerUrl` / `parse_url`）口径未变，与默认端口同一代码路径。
- `server.sh` 仅 `sh -n` 语法检查（本机为 Windows，脚本面向 macOS）。

## 非目标

- 不设超时、不显示构建进度（进度在 `.stats/server.log`）。
- 不改服务本身（rebuild 先于监听的顺序保持不变）。
