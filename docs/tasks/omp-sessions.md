# OMP 第七类 agent：session 索引

## 背景

OMP（oh-my-pi）会话日志与 Pi 同源同构：`~/.omp/agent/sessions/<cwd-slug>/<时间戳>_<uuid>.jsonl`，每文件一个会话（文件名、消息类型、usage 字段与 Pi 一致）。本机已有数据，应作为第七类 agent 纳入统计。

## 数据来源与口径

- 来源：`~/.omp/agent/sessions/**/*.jsonl`（隐藏文件 `.jsonl.lock.os` 与 `*.bash.log` 目录不匹配 `*.jsonl`，天然排除）。
- 用量：assistant `message.usage` = `{input, cacheWrite, cacheRead, output, cost, totalTokens}`。与 Pi 的差别：**无 `reasoning` 字段**，实测 48/48 条 `totalTokens = input + cacheWrite + cacheRead + output`，即 `output` 已含思考，不再相加。
- 模型：assistant `message.model`（短名，provider 在 `message.provider` 单独记录）；`model_change` 记录写 `model`（`provider/model` 全名，Pi 写 `modelId`），作消息缺 model 时的兜底。
- 去重：与 Pi 相同，按 (消息 id, 时间戳, 模型)；同一 id 重复出现只计一次。
- 轮次：user `text` 为输入；assistant 的 `thinking`/`toolCall`/`text` 与 `toolResult` 角色为输出，复用 Pi 分支的 blocks() 归类（思考/工具调用/工具结果/正文）。
- 注入消息：OMP 的 harness 注入不进 user 消息正文（实测用户消息均为原文）；`isMeta`/`isCompactSummary`/`isSidechain` 标志与 Pi 共用同一处理。

## 改动

- `stats_data.py`：`TOOLS` 增加 `'omp'`；`discover` 增加 pattern；`usage_values` 增加 omp 行（`output` 不加 reasoning）；`read_file` 的 claude/pi 分支纳入 omp，`model_change` 兜底改读 `modelId` 或 `model`（Pi 该字段为 null，行为不变）。
- README「数据来源与口径」、AGENTS.md：六类 → 七类。
- 前端无 agent 硬编码（下拉来自接口），不改。

## 验收

- 契约测试：omp 一个文件解析出 1 轮（user + thinking + toolCall + toolResult + text）、用量 new/cc/cr/out 正确、重复 id 只计一次。
- `--reindex` 后 `/api/meta` 的 `sources` 含 `omp`，`/api/usage?agent=omp` 有数；页面 agent 下拉出现 omp 且图表可选。
- 全量测试与 `node --check` 通过。
