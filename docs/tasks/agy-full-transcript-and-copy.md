# agy 优先索引完整转录与前端正文复制增强

## 背景

`agy`（Antigravity CLI）的精简转录 `transcript.jsonl` 不仅截断工具输出，还会对超长的 `PLANNER_RESPONSE.content`（正文）、`thinking`（思考）、`tool_calls`、`CHECKPOINT` 和 `USER_INPUT` 插入 `<truncated N bytes>` 标记。同目录下均并存结构一致的完整转录 `transcript_full.jsonl`。

此前 [agy-sessions.md](agy-sessions.md) 仅索引 `transcript.jsonl`，导致会话详情展示、全文搜索、单段复制与 Markdown 导出都只能看到被截断后的内容；同时前端在「Agent 响应」层级缺少一键复制全部正文按钮，`copyText` 在 Clipboard API 被拒时也会静默失败而未降级。

## 方案

1. **采集层（`stats_data.py`）**：`discover()` 对每个 `agy` 会话目录同时发现 `transcript_full.jsonl` 与 `transcript.jsonl`，按目录去重并优先选择 `transcript_full.jsonl`，缺失时回退 `transcript.jsonl`，保证每会话只索引 1 个文件、不重复计用量。
2. **增量刷新顺序（`stats_server.py`）**：`update_index()` 先处理已消失文件（`delete_file_rows`），再解析新增/变更文件（`parse_into`）。避免同会话文件切换（如 `transcript.jsonl` → `transcript_full.jsonl`、dsh 版本升级、Codex 归档迁移）时，新文件因与待删旧文件的 `turns.identity` 冲突而被跳过、随后旧文件又被删除导致轮次丢失。删除旧文件会置 `revised=True`，从而刷新 `export_revision`、带动远端下次同步自动更新为完整全文。
3. **前端复制增强（`static/app.js`）**：
   - 在 `details.response > summary` 增加「复制正文」按钮（该轮存在 `正文` 段时显示），将全部正文段按原顺序以双换行拼接复制。
   - 修复 `copyText()`：`navigator.clipboard.writeText` 失败时回退到 `textarea + execCommand('copy')`，并依据返回值反馈「已复制 / 复制失败」；避免连续点击覆盖原始按钮文案。
4. **文档与测试**：更新 `README.md` 的 agy 数据来源口径，在 `tests/test_contracts.py` 补充完整转录优先、缺失回退及增量切换刷新测试。

## 验收

- `.venv/bin/python -m unittest discover -s tests -v` 全量通过。
- `node --check static/app.js` 通过。
- 新增契约测试覆盖：同目录并存时优先读 `transcript_full.jsonl` 且用量不重复、仅存在 `transcript.jsonl` 时回退正常、已有 `transcript.jsonl` 索引升级为 `transcript_full.jsonl` 时 `update_index` 保留轮次并替换为完整正文且刷新 `export_revision`。

## 实测（2026-11-20，本机 macOS）

- 契约测试 71 项全部通过（含新增 2 项 agy 完整转录优先/回退与增量切换测试），`node --check static/app.js` 通过。
- 重启本机服务并触发增量刷新后，本机索引 `agy` 全部 79/79 轮已自动切至 `transcript_full.jsonl`；抽查此前被截断的会话，`/api/turn` 返回正文由 2,742 字符恢复为完整 10,863 字符。
- 远端主机需在远端侧更新本版本并刷新索引后，本机再次同步即可自动更新远端 `agy` 会话为完整全文。

