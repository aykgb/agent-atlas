# OpenCode 新版 schema：session_message / session_v2

来源：OpenCode 升级后 `~/.local/share/opencode/opencode.db` 改用新表，旧解析器（message/part）读不到新会话。

## 现状与落点

- 新版按会话迁移：已迁移会话在 `session_message(id, session_id, type, seq, time_created, data)` + `session_v2(id, model)`，未迁移的旧轮次仍在 `message`/`part`，两套表可并存。
- v2 行结构：`type` 为 user/assistant/idle/synthetic 等；user 的 data 含 `text`，assistant 的 data 含 `model.id`、`content`（part 数组：reasoning / text / tool，tool 结果在 `state.content`，旧版在 `state.output`）、`tokens`（input/output/reasoning/cache.read/cache.write，与 v1 同形）。
- `usage_values('opencode')` 对 tokens 的口径不变，v2 直接复用。

## 方案

- `read_opencode` 先查 `sqlite_master` 建表名集合：`message`/`part` 存在（或 `session_message` 不存在）时读 v1；`session_message` 存在时读 v2。两套并存时都读，用量按 `(agent, key=消息id)` 取 MAX 去重，口径不变。
- v1 逻辑原样抽到 `read_opencode_v1`；新增 `read_opencode_v2`：user 取 `data.text` 成轮（模型取 `session_v2.model.id`），assistant 输出用 `opencode_blocks`（每 part 复用 `blocks()`，tool part 追加 `state.content`/`state.output` 为工具结果），usage 取 `data.tokens`。
- v2 data JSON 解析失败记 warning 跳过，不中断。
- README「数据来源与口径」同步：OpenCode 行改为新旧两套都读，去重段补「并存时两套都读、按消息标识去重」。

## 验收

1. 纯 v2 库：轮次、输出段（思考/工具调用/工具结果/正文）、用量正确；synthetic/idle 行不入轮次。
2. v1+v2 并存库：两套轮次与用量都读到，不重复。
3. `unittest`（38 项）+ `node --check` 通过。

## 非目标

- 不改 v1 解析与用量口径；不做 v1→v2 迁移。
