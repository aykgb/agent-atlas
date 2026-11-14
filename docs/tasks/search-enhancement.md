# 会话搜索功能优化与增强

来源：全库 `/api/search` 实测与前端交互分析，解决多源混合检索慢、筛选维度单一、摘要只看首词且重复、展开轮次找不到高亮位置等痛点，并保持 `SCHEMA_VERSION = 5` 与既有远端索引兼容。

## 目标与改动范围

### 1. 后端检索性能与口径增强（`stats_server.py`）
- **查询性能修复**：
  - `q=''` 或常规非相关度查询时，把 `COUNT(*) OVER ()` 拆分为独立的 `SELECT count(*)` 与 `ORDER BY ... LIMIT ?`，恢复 `turn_time(timestamp DESC, id DESC)` 索引早停，避免全表扫描与临时 B-Tree 排序。
  - 短词与混合词查询采用 `input LIKE` 短路及 `output` 原文预过滤，非必要不解码 `json_each(turns.output)`；`scope` 限定 `正文` / `思考` / `工具` 时先用字段存在性预筛选。
- **查询语法与多维度筛选**：
  - `q` 支持双引号 `"..."` 精确短语与空格分隔普通关键词混合检索（保持全部命中语义）。
  - 新增 `exclude` 参数：独立排除词（同样支持引号短语，空格分隔），避免 `-flag` 语法误伤 `-v` / `-m` 等命令行参数检索。
  - 新增 `scope` 参数：`all`（默认，全轮文本）/ `input`（仅用户输入）/ `text`（仅模型响应正文）/ `thinking`（仅思考过程）/ `tool`（仅工具调用与工具结果）。
  - 新增 `source` 参数（`local` 或远端 `key`）、`session` 参数（会话 ID 大小写不敏感子串过滤）；`/api/meta` 补充 `search_sources` 供前端下拉框渲染。
  - 新增 `sort` 参数：`newest`（默认最新优先）/ `oldest`（最早优先）/ `relevance`（按输入加权、正文次之、多关键词命中频次与时间综合排序）。
  - 新增 `group` 参数：`turn`（默认按轮次分页）或 `session`（按 `(key, agent, session)` 聚合会话分页，返回命中轮次数、首末时间与代表性命中轮次，并带 `total_turns`）。
- **高精度命中摘要与结构化定位**：
  - 搜索行保留向后兼容的 `preview` 与 `match`，新增 `hit_kinds`（命中区块列表）、`matches`（多关键词分段上下文化摘要，自动避开与 `preview` 前部重复的片段）、`part_count` 与 `input_len`；从词频带入 `term` 时同样提取定位摘要。

### 2. 前端搜索体验与定位导航（`static/index.html`、`static/app.js`、`static/style.css`）
- **筛选与视图控制**：
  - 新增作用域选择（全部 / 仅用户输入 / 仅正文 / 仅思考 / 仅工具）、来源主机选择、排序方式（最新 / 最早 / 相关度）、视图切换（按轮次 / 按会话分组）、会话过滤输入框、`排除词` 输入框及用户输入长度快捷档位（不限 / 短 ≤30 / 中 31–300 / 长 ≥301 / 自定义）。
  - 所有新筛选条件同步纳入 URL Hash（刷新保持与前进/后退）与「重置筛选」，支持在搜索页按 `/` 快速聚焦搜索框；会话详情头部支持「在此会话内搜索」。
- **卡片信息与展开定位**：
  - 按轮次卡片标注会话 ID 缩写与「仅搜此会话」快捷筛选、「完整会话」入口、命中区域标签（`input` / `text` / `thinking` / `tool call` / `tool result`）与多片段高亮摘要。
  - 按会话分组视图展示每个会话的匹配轮数、时间跨度、展开代表性轮次卡片及「在完整会话中查看 / 仅搜此会话」入口。
  - 展开搜索结果轮次时，自动展开 `Agent 响应` 及包含命中词的响应分段并标出「命中」徽章；顶部提供高亮导航栏（「↑ 上一处 / ↓ 下一处」及 `1 / N` 计数），支持平滑滚动聚焦到目标 `<mark>`（含引文短语及排除词边界不误高亮）。

### 3. 文档与契约测试（`README.md`、`tests/test_contracts.py`）
- 更新 `README.md`「页面操作」搜索说明。
- 在 `tests/test_contracts.py` 补充契约测试覆盖：引号精确短语、独立排除词、5 种 `scope` 精确过滤、`source` 与 `session` 过滤、`oldest`/`relevance` 排序、`group='session'` 聚合与分页、`hit_kinds`/`matches` 结构化摘要（含防 `preview` 重复与 `term` 摘要）。

## 验收

1. 在现有本机 + 远端库实测下，`q=''`、`代理 mcp` 与短词搜索较优化前显著提速，且 55 项旧契约与新增搜索契约全通过。
2. 引号短语 `"a b"`、排除词 `exclude`、`scope`（`input`/`text`/`thinking`/`tool`）、`source`、`session`、`sort`（`newest`/`oldest`/`relevance`）及 `group='session'` 行为准确无误。
3. 前端展开搜索命中轮次后自动展开命中分段、可「上一处/下一处」高亮跳转，桌面与移动端均无布局溢出与控制台报错。
