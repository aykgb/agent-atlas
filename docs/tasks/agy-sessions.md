# agy（Antigravity CLI）第八类 agent：session 索引

## 背景

本机安装了 Antigravity CLI（`agy`），会话落在 `~/.gemini/antigravity-cli/`。`agy` 是独立客户端，用量不在现有七类日志里，需要单独接入。

## 数据来源与口径

- 来源：`~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript.jsonl`，每会话一份。`transcript_full.jsonl` 是同记录、工具输出未截断的版本，不重复索引；`chunks/` 是同一数据的轮转分片，不索引。
- 用量：每条 `PLANNER_RESPONSE` / `CHECKPOINT` 记录带 `created_at`、`input_tokens`、`cache_read_tokens`、`output_tokens`。`input_tokens` 不含缓存读取（实测下一轮 `cache_read_tokens` ≈ 本轮 `input_tokens + cache_read_tokens`）；`output_tokens` 已含思考（只有 thinking、没有正文的 step，output 仍非零）。四项口径：新增输入 = `input_tokens`，缓存写入 = 0，缓存读取 = `cache_read_tokens`，输出 = `output_tokens`。
- 模型：转录没有模型字段。读同会话 `conversations/<id>.db` 的 `gen_metadata`（protobuf wire format）：`2` 是 step 范围（取首值），`1.19` 是模型 key（如 `gemini-4-argon`）。DB 缺失或解不出时记 unknown；`CHECKPOINT` 没有对应 gen 行，沿用当前模型（与 dsh 压缩摘要一致）。
- 轮次：`USER_INPUT` 从 `<USER_REQUEST>…</USER_REQUEST>` 中取正文，`<ADDITIONAL_METADATA>` / `<USER_SETTINGS_CHANGE>` 不进轮次；`PLANNER_RESPONSE` 的 `thinking` / `tool_calls` / `content` 分别为思考 / 工具调用 / 正文；`GENERIC` 为工具结果；`CHECKPOINT` 摘要单独成轮、可搜索、不计词频；`SYSTEM_MESSAGE`（harness 系统提示）不入索引。
- 去重：按 (会话, step, 记录类型)，同一 step 只计一次。
- 增量：以 `transcript.jsonl` 为索引文件，mtime/size 变化触发重解析。0 步的会话没有 transcript，不产生条目；会话进行中文件持续追加，最后一行可能是半条 JSON，按现有 `read_jsonl` 逐行跳过。

## 改动

- `stats_data.py`：`TOOLS` 增加 `agy`；`discover` 增加 pattern；`usage_values` 增加 agy 分支；`read_file` 增加 agy 分支；新增 `<USER_REQUEST>` 提取与 protobuf 变长整数/字段解析（仅标准库）。
- `README.md`、`CLAUDE.md`、`static/index.html`：agent 列表与口径补充 agy。
- `tests/test_contracts.py`：合成 transcript + SQLite `gen_metadata`，覆盖用量、模型、轮次、`<USER_REQUEST>` 提取、CHECKPOINT 与去重。

## 验收

- 契约测试与 `node --check static/app.js` 通过。
- `--reindex` 后 `/api/meta` 的 `sources` 含 `agy`，`/api/usage?agent=agy` 有数，会话列表可打开。
- 本机实测：两个有内容的会话，token 与 `transcript.jsonl` 逐条一致；模型名解出 `gemini-4-argon`。

## 实测（2026-11-04，本机 Windows）

- 契约测试 48 项（1 项 skip：本机无系统 `zstd`，dsh 压缩用例跳过），`node --check static/app.js` 通过。新增 `test_agy_reads_transcript_model_protobuf_and_checkpoint` 与 `test_agy_missing_model_db_still_reads_usage`。
- 重启服务并 `POST /api/refresh` 后：`/api/meta` 的 `agents` 含 `agy`、`sources.agy=2`；`/api/usage?agent=agy` 为 `gemini-4-argon` 一行；`/api/sessions?agent=agy` 两个会话（6 轮 / 2 轮）可打开。临时索引快照为 new 1,096,591 / cr 13,356,605 / out 107,358 / 121 次；会话仍在写入，数值会继续增长。
- 采样核对：`a19ece5d` 的 11 条用量与 `transcript.jsonl` 完全一致（new/cr/out），模型解出 `gemini-4-argon`。

## 非目标

- 不改远端导出协议：agy 用量随 `file_usage` 进入现有 `usage` / `session_usage`，同步无需新增字段。
- 不索引 `transcript_full.jsonl` 与 `chunks/`（与 `transcript.jsonl` 是同一份数据的未截断/分片版本）。
- 不解 `step_payload` 里的正文 protobuf；正文直接来自转录 JSONL。
