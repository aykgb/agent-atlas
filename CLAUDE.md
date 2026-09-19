# CLAUDE.md

统计本机六类 agent（Claude、Codex、Pi、OpenCode、Grok、dsh）的 token 用量：Python 标准库 + jieba 后端，原生 JavaScript + SVG 前端，SQLite FTS5 索引。

## Project map

- `stats_server.py` — HTTP 服务，默认 127.0.0.1:18763；建索引与查询
- `stats_data.py` — 日志解析、去重与聚合（六类 agent 日志）
- `stats-today.py` — CLI，直接读日志，不依赖索引
- `server.sh` / `server.ps1` — 服务控制：start / stop / restart / status（macOS / Windows），PID 与日志存于 .stats/
- `static/` — 前端：index.html、app.js、style.css
- `tests/test_contracts.py` — 契约测试
- `README.md` — 用法、数据来源与口径（解析与聚合逻辑的规格）、验证
- `STATUS.md` — 任务面板：任务与状态（任务完成时更新；说 “status” 时由 skill 读取）
- `skills/status/SKILL.md` — 项目状态 skill：读 STATUS.md 并采集服务/索引/远端/git 实况
- `docs/tasks/` — 任务文档：方案、实测与验收标准
- `remotes.json` — 远端汇入配置（运行时生成，不入库）
- `words-exclude.txt` — 词频排除词（运行时生成，不入库）
- `.stats/` — 派生索引（本机 index*.sqlite、每台远端 remote-*.sqlite）与服务 PID/日志，可删，下次启动自动重建（远端需重新同步）

<important if="you need to run commands to install, start, or verify">

| 命令 | 用途 |
|---|---|
| `uv sync` | 安装依赖到 .venv |
| `.venv/bin/python stats_server.py` | 启动服务（端口 18763） |
| `./server.sh start \| stop \| restart \| status` | 服务控制（后台运行，日志 .stats/server.log） |
| `.\server.ps1 start \| stop \| restart \| status` | 同上（Windows） |
| `.venv/bin/python stats_server.py --reindex` | 重建索引后退出 |
| `.venv/bin/python stats-today.py [--week \| --days N]` | CLI 统计 |
| `.venv/bin/python -m unittest discover -s tests -v` | 运行测试 |
| `node --check static/app.js` | 前端语法检查 |
</important>

<important if="you are adding a feature or changing behavior">
- 先写任务文档到 `docs/tasks/`，并在 `STATUS.md` 表格登记（状态留空），再写代码；完成后把该行状态改为 ✅。
- Bug 修复与任务文档范围内的重构直接做，不新建任务文档。
</important>

<important if="you are modifying log parsing, dedup, or aggregation logic">
- README「数据来源与口径」一节是规格，tests/ 是它的可执行版本；改动时同步更新 README 与测试。
- dsh 的 `inputTokens` 不含缓存读取、`outputTokens` 已含思考；注入消息并入当前轮「上下文」段、压缩摘要单独成轮，都不计词频。
</important>

<important if="you are about to commit">
- 先运行 README「验证」一节的两条命令（测试 + node --check），全部通过。
- 提交内容不含本地 agent session 数据（.stats/ 索引、words-exclude.txt、remotes.json、日志原文）；测试夹具只用合成内容。
</important>

<important if="you are adding dependencies or frontend resources">
- Python 侧仅标准库与 jieba；前端为原生 JavaScript + SVG，不引入 npm 或第三方网页资源。
- dsh 会话经系统 `zstd` 命令解压（外部命令，不算 Python 依赖）；缺失时跳过 dsh 并在页脚提示，不报错。
</important>

<important if="you are modifying the network layer of stats_server.py (binding, headers, routing)">
- 默认仅监听 127.0.0.1；开放监听与远端 Host 须经 `--allow-host` 放行名单（默认含 100.64.216.70），Host/Origin 校验始终生效（tests 已固定）。
- 写操作（POST）仅限本机：客户端地址、Host 与代理转发头均须为回环。
</important>

<important if="you are modifying remote import (remotes.json, /api/export, sync_remote)">
- 本机索引只含本机数据；每台远端一个 `.stats/remote-*.sqlite`。停用只在查询时排除、不删数据；移除才删文件。
- 汇入由本地服务主动 GET 远端 `/api/export`；按 cursor + 轮次指纹增量，远端重建时自动全量重拉；失败整事务回滚并记入该远端库的 `sync_error`。
- 查询时合并本机与启用中的远端（用量/会话/词频按勾选）；轮次 id 形如 `<key>:<id>`，详情按前缀路由。
- `remotes.json` 是本地个人配置（gitignore）；POST /api/remotes、/api/sync 与其它写操作一样仅限本机。
- 勾选 words 隐含 search：词频轮次必须可搜索、可打开。
</important>

<important if="you are modifying the local index or refresh (rebuild, update_index)">
- 刷新为增量：meta 的 `indexed_at`（毫秒水位）加 `files(path,size)` 判断，只重解析变化文件；首次运行、schema 升级与 `--reindex` 才全量重建。
- 服务每 30 分钟自动检查（`changed_files` 作闸门、`watch_index` 线程），有变化才增量更新本机索引，不自动同步远端。
- 用量按文件存 `file_usage`、查询前聚合；跨文件重复按 `(agent,key)` 取 MAX，保持旧口径。
</important>

<important if="you are reading or writing agent logs or the index">
- 写入只发生在 .stats/ 索引；agent 日志一律只读。
</important>
