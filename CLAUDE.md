# CLAUDE.md

统计本机五类 agent（Claude、Codex、Pi、OpenCode、Grok）的 token 用量：Python 标准库 + jieba 后端，原生 JavaScript + SVG 前端，SQLite FTS5 索引。

## Project map

- `stats_server.py` — HTTP 服务，默认 127.0.0.1:18763；建索引与查询
- `stats_data.py` — 日志解析、去重与聚合（五类 agent 日志）
- `stats-today.py` — CLI，直接读日志，不依赖索引
- `server.sh` — 服务控制：start / stop / restart / status，PID 与日志存于 .stats/
- `static/` — 前端：index.html、app.js、style.css
- `tests/test_contracts.py` — 契约测试
- `README.md` — 用法、数据来源与口径（解析与聚合逻辑的规格）、验证
- `remotes.json` — 远端汇入配置（运行时生成，不入库）
- `.stats/` — 派生索引与服务 PID/日志，可删，下次启动自动重建

<important if="you need to run commands to install, start, or verify">

| 命令 | 用途 |
|---|---|
| `uv sync` | 安装依赖到 .venv |
| `.venv/bin/python stats_server.py` | 启动服务（端口 18763） |
| `./server.sh start \| stop \| restart \| status` | 服务控制（后台运行，日志 .stats/server.log） |
| `.venv/bin/python stats_server.py --reindex` | 重建索引后退出 |
| `.venv/bin/python stats-today.py [--week \| --days N]` | CLI 统计 |
| `.venv/bin/python -m unittest discover -s tests -v` | 运行测试 |
| `node --check static/app.js` | 前端语法检查 |
</important>

<important if="you are modifying log parsing, dedup, or aggregation logic">
- README「数据来源与口径」一节是规格，tests/ 是它的可执行版本；改动时同步更新 README 与测试。
</important>

<important if="you are about to commit">
- 先运行 README「验证」一节的两条命令（测试 + node --check），全部通过。
- 提交内容不含本地 agent session 数据（.stats/ 索引、words-exclude.txt、日志原文）；测试夹具只用合成内容。
</important>

<important if="you are adding dependencies or frontend resources">
- Python 侧仅标准库与 jieba；前端为原生 JavaScript + SVG，不引入 npm 或第三方网页资源。
</important>

<important if="you are modifying the network layer of stats_server.py (binding, headers, routing)">
- 默认仅监听 127.0.0.1；开放监听与远端 Host 须经 `--allow-host` 放行名单（默认含 100.64.216.70），Host/Origin 校验始终生效（tests 已固定）。
- 写操作（POST）仅限本机：客户端地址、Host 与代理转发头均须为回环。
</important>

<important if="you are modifying remote import (remotes.json, /api/export, import_remote)">
- 汇入由本地服务主动 GET 远端 `/api/export` 并写入本地索引；远端不可达时重建必须继续，只在 warnings 与 meta.remotes 标记失败。
- `remotes.json` 是本地个人配置（gitignore）；POST /api/remotes 与其它写操作一样仅限本机。
- 勾选 words 隐含 search：词频轮次必须可搜索、可打开。
</important>

<important if="you are reading or writing agent logs or the index">
- 写入只发生在 .stats/ 索引；agent 日志一律只读。
</important>
