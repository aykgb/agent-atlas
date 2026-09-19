# Agent 使用统计

统计本机 Claude、Codex、Pi、OpenCode、Grok、dsh 的模型 token 用量，并提供会话搜索和用户输入词频。这里的 agent 指这六类客户端；一轮会话指一条用户输入及其后续输出，直到下一条用户输入。Token 是模型处理内容的计量单位，页面展示日志用量，不计算费用。

## 启动

需要 uv（Python 3.12 或更新版本，缺失时由 uv 自动安装）；SQLite 需支持 FTS5 全文搜索扩展及 trigram 分词器，且版本 ≥ 3.43（contentless FTS 的删除支持）。dsh 会话为 zstd 压缩，需要系统有 `zstd` 命令（macOS 可用 `brew install zstd`）；缺失时跳过 dsh 并在页脚提示。

```sh
cd $HOME/agent-atlas
uv sync
.venv/bin/python stats_server.py
```

打开 <http://127.0.0.1:18763>。默认只监听本机；Ctrl+C 停止，或用 `./server.sh start | stop | restart | status` 后台运行（Windows 用 `server.ps1`，命令相同；PID 与日志都在 `.stats/`）。端口被占用时使用 `--port 18764`。

远端经反向代理访问时，用 `--host` 指定监听地址（`0.0.0.0`、局域网或 Tailscale 的 IP），用 `--allow-host` 放行访问用的域名或 IP（可重复；默认已放行 Tailscale 地址 `100.64.216.70`）：

```sh
.venv/bin/python stats_server.py --host 0.0.0.0 --allow-host stats.example.com
```

Host 与 Origin 始终校验，只接受回环地址和放行名单。GET 查询对放行的远端开放；刷新索引、编辑排除词、配置与同步远端仅限本机直连，远端页面会隐藏这些操作。服务本身不带鉴权且远端可见全部会话原文，访问控制交给 Tailscale、内网或反向代理；代理需透传原始 Host 与 X-Forwarded-For / X-Real-IP，否则远端请求会被误判为本机。

筛选栏下方的「远端主机」面板可配置多台远端（主机 + 端口），每台独立启用/停用，并分别选择汇入用量、会话索引、词频。本机索引与远端数据分开存放：`.stats/index.sqlite` 只含本机日志，每台远端一个 `.stats/remote-*.sqlite`。停用只在查询时排除该远端，不删除已同步数据；移除才删除对应文件。远端需运行同一版本并放行本机 Host，端口可用 Tailscale 地址或 SSH 隧道映射。新增或点「同步」时主动 GET 远端 `/api/export` 增量拉取：远端索引未重建时只取新增轮次，重建过（轮次指纹对不上）则自动全量重拉。查询时合并本机与启用中的远端：用量相加、词频相加、搜索按时间归并分页、轮次 id 带来源前缀，详情按前缀路由。远端不可达时保留上次数据，只在面板与页脚标记失败。词频需要会话索引；勾选词频会自动包含会话。远端配置存于仓库根目录 `remotes.json`（JSON 数组，可手工编辑，不入库）。

首次启动全量建立索引；之后点击「刷新数据」按增量更新：只重解析有变化的 session 文件（meta 记录毫秒水位 `indexed_at`，结合 `files` 表的大小判断），并同步启用中的远端。服务运行期间每 30 分钟自动检查一次，有新增、变化或消失的 session 文件才更新本机索引（只更新本机，不自动同步远端）。完成前仍可查询旧索引。`.venv/bin/python stats_server.py --reindex` 强制全量重建。

原始日志只读。索引在 `.stats/index.sqlite`，包含会话原文，不上传远端，不加载第三方网页资源。删除 `.stats` 后会在下次启动重建（远端数据需重新同步）。中文分词只依赖 jieba，其余后端使用 Python 标准库，前端使用原生 JavaScript 和 SVG。

## 命令行

每次直接读取日志，不依赖 jieba 或网页索引。

```sh
python3 stats-today.py
python3 stats-today.py 2026-09-18
python3 stats-today.py --week
python3 stats-today.py --days 30
python3 stats-today.py --days 30 --json
```

默认显示 agent × 模型明细；多日查询额外显示每日各 agent 总量。JSON 返回日期、agent、模型三个维度聚合及读取提示。两个入口均支持 `--home /path/to/home` 指定其他日志根目录；服务使用 `--home` 时索引单独存放（`index-*.sqlite`），不影响默认索引。

## 页面操作

- **用量概览**：最近 N 天、周或月。周从周一开始，月从一号开始，当前周期截至今天。支持 agent、模型、两者组合的堆叠柱状图和环形饼图。悬停柱段或环形扇区即时提示周期、分组、token 数与占比（键盘聚焦同样触发）。超过七个分组归入「其他」，明细表保留所有模型。
- **会话搜索**：搜索输入、正文、思考、工具调用和结果。空格分隔的关键词须在同一轮全部出现，不要求在同一条消息中。可按用户输入字符数过滤（最短 / 最长，含端点，留空不限）。每页 20 轮；展开轮次后，输出按类别和段落折叠，段落序号保留原顺序。原文按纯文本显示。
- **输入词频**：仅对人写的用户输入分词；子 agent、审查会话、压缩摘要，以及注入的环境、插件、skill、仓库指令（AGENTS.md）与命令输出等机器生成的 user 消息不计入（仍可搜索）。英文忽略大小写，过滤单字、纯数字和常见中英文停用词。横条展示出现次数，同时显示覆盖轮次。点击词语通过倒排索引定位输入包含该词的轮次。词条「×」或输入框可排除关键词，点击已排除的标签恢复；列表存于仓库根目录 `words-exclude.txt`（一行一词，可手工编辑），只影响词频，不影响搜索。
- Agent 和模型筛选在三个页面共用。搜索、词频共用起止日期，默认全部时间；用量页独立使用最近 N 个周期。

## 数据来源与口径

| Agent | 来源 | 用量字段 |
|---|---|---|
| Claude | `~/.claude/projects/**/*.jsonl` | assistant 的 model、usage |
| Codex | `~/.codex/sessions/**/*.jsonl`、`~/.codex/archived_sessions/**/*.jsonl` | turn_context / world_state 模型；优先 token_usage_record，旧轮次使用 token_count |
| Pi | `~/.pi/agent/sessions/**/*.jsonl` | assistant 的 model、usage |
| OpenCode | `~/.local/share/opencode/opencode.db` | message 的 modelID、tokens；part 提供正文、工具调用与结果 |
| Grok | `~/.grok/sessions/**/updates.jsonl` | turn_completed.usage.modelUsage 按模型拆分，缺少拆分时使用当前模型 |
| dsh | `~/.dsh/sessions/*/session-*/session*.jsonl.zstd`（版本号高者优先） | assistant/message 的 model、usage；压缩摘要调用计入 compaction/summary 的 model、usage |

总量 = 新增输入 + 缓存写入 + 缓存读取 + 输出。新增输入不含缓存；Codex、Grok 输出已含思考，不重复相加；Pi、OpenCode 单独记录的 reasoning 与 output 相加。日期统一为本地时区。调用次数来自用量记录，Grok 使用 modelCalls。

Claude 同一消息的流式 usage 取各字段最大值，只计一次调用；Codex 同轮逐请求与汇总事件不重复计数，旧格式重复累计快照跳过；Pi 按消息标识、时间、模型去重；Grok 按事件标识和模型去重；OpenCode 工具调用的参数记入调用块，输出只记入工具结果，不重复；dsh 的 `inputTokens` 不含缓存读取、`outputTokens` 已含思考（totalTokens 可验证），按 assistant/message 的消息 id 去重，压缩摘要调用单独按 compactionId 计入。缺少模型时标为 unknown。零用量或错误消息可保留调用记录。

只索引本机存在且可读的数据，不补算缺失历史。损坏或非对象 JSON 行跳过并在页脚提示。仅展示可见的思考文本，不解密隐藏内容。轮次按日志顺序划分；历史分叉复制的输入可能分别出现在不同会话。Codex 有明确内容类型元数据时排除自动注入的环境和规则，缺少标记的旧日志保留原始 user 消息。自动审查、子 agent 会话、压缩摘要，以及注入的环境、插件、skill、仓库指令（AGENTS.md）与命令输出等机器生成的 user 消息保留在搜索中，不计入词频。命令包装消息（如 /clear、/model）不成轮次；有后续输出的命令（如 /improve-writing）保留轮次并显示为可读命令，同样不计入词频。dsh 的注入消息（插件、skill 目录、AGENTS.md）并入当前轮的「上下文」段，压缩摘要单独成轮，都可搜索、不计词频；`system/message`（harness 系统提示）与标题生成请求不入索引。

索引含字符三元组全文索引（contentless，不重复保存正文），体积可能为数百 MB。三个字符及以上的搜索走全文索引，不足三个字符的按输入与输出原文拼接后字面扫描，保证中文短词可匹配。输入词频倒排表为 `terms(term, turn_id, count)`。刷新为增量：只重解析 mtime 晚于水位或大小变化的文件，删除已消失文件的行；用量按文件记录后在查询前聚合。首次运行、schema 升级与 `--reindex` 走全量重建（临时文件后原子替换）。远端数据不写入本机索引，各自独立存放；同步按 `cursor` + 轮次指纹增量，远端重建导致 id 重排时自动全量重拉，失败整事务回滚保留上次数据，状态记录在该远端库的 `meta`。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
```

测试覆盖六类日志的归属与去重、思考计量、输入输出配对、中文搜索与提示词长度过滤、词频范围、日历边界、本机增量更新（水位与 size 判断、文件删除）、dsh 的 zstd 解压与用量口径（缺失 zstd 时降级告警）、远端指纹增量与重建回退、同步失败保留、查询合并与轮次路由，以及 HTTP 的 Host / Origin 白名单、远端只读边界与静态资源的 ETag / 304。
