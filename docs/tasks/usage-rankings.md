# 用量概览：Agent 与模型使用排名

来源：2026-09-19 用户需求。在「用量概览」增加两个排名：agent 使用排名、模型使用排名。

## 现状与落点

- `renderUsage`（static/app.js）已把 `data.rows` 按 agent+模型汇总成 `rows` Map，并算出各指标合计与总量。
- 落点在「用量趋势 / 用量占比」之后、「Agent × 模型明细」之前：新增 `.rank-grid` 两栏并排，窄屏单列。

## 口径

- 与用量页同源：共享 agent / 模型筛选与最近 N 个周期；排名指标为总 token（新增输入 + 缓存写入 + 缓存读取 + 输出）。
- 模型名不区分大小写（详见下方「修正」）：同一模型在不同日志里写法不一（pi 的 `Qwen3.8-Flash-Next-IQ3_S` 与 `qwen3.8-flash-next-iq3_s`），按小写归一合并计总量，显示名取用量最多的写法；agent 榜不受影响。
- 榜内按总量降序，同量按名称升序；条形长度按该榜最大值归一，右侧显示压缩值与占总量百分比。
- 悬停行显示精确 token 数与占比；模型名过长省略号，`title` 给全名。空范围复用现有空态文案。

## 实现要点

- index.html：`#usage` 内新增 `.rank-grid` 两个面板（`#agentRank`、`#modelRank`）。
- app.js：新增 `renderRanking(id, entries, total)`；`renderUsage` 里从已汇总的 `rows` 按字段再聚合。
- style.css：`.rank-grid / .rank-row / .rank-index / .rank-name / .rank-track / .rank-fill / .rank-value`，超长列表滚动。

## 验收（CDP 冒烟）

1. 默认范围：两榜名称与排序和明细表一致；agent 榜总量合计 = 指标卡总量，百分比合计 ≈ 100%。
2. 切换周期（天 / 周 / 月）或 agent / 模型筛选后，两榜同步变化。
3. 空数据显示空态；模型多时列表可滚动；无新增第三方资源，`node --check` 通过。

## 实测（2026-09-19，CDP 冒烟）

- 默认范围：两榜与 `state.usage` 聚合逐项一致、按总量降序；agent 榜合计 = 指标卡总量，占比合计 ≈ 100%。
- 切周、按 agent 筛选后同步重算（claude 单行，其 6 个模型）；空数据与恢复渲染正常。
- 500px 宽下单列、无行溢出，模型榜超高可滚动；无控制台报错，`node --check` 通过。

## 修正（2026-10-08）：模型名合并范围扩到整页

大小写归一从模型榜扩到模型相关的一切展示与筛选（实现改为 `state.modelNames`：由 `/api/meta` 的模型列表得到 小写 -> 显示名，前端 `modelOf()` 统一取值）：

- 前端（static/app.js）：明细表按 agent + 小写模型名聚合；趋势与占比的 `model` / `pair` 分组同样取显示名；两个排名继承已聚合的 `rows`。
- 后端（stats_server.py）：`bounds()` 的模型筛选改 `lower(model) = lower(?)`（`/api/usage`、`/api/sessions`），`scoped_bounds()` 的多模型列匹配两边 `lower()`（`/api/search`）；`/api/meta` 的模型下拉按小写归一，显示名取用量最多的写法（`merged_models()`）。
- 取显示名统一按全索引用量，而不是当前范围，否则下拉选项与图表标签、下钻回写的值可能对不上。
- 不变：`/api/usage` 与 `/api/export` 的行保留日志里的原始写法（汇总口径不变）；搜索与会话详情里的轮次模型名不重写；agent 榜不受影响。

契约测试：`test_model_filter_and_model_list_ignore_case`（45 项）。CDP 实测 2026-10-01~10-31：模型榜与明细表均由两行 `qwen3.8-flash-next-iq3_s` 23.44M + `Qwen3.8-Flash-Next-IQ3_S` 16.61M 合并为一行 40,049,369 tokens（7.1%）；模型占比与 Agent × 模型分组同样只剩一项；双击该柱段下钻后筛选到该模型、总量与合并值一致，占比合计 100.0%。

## 非目标

- 后端接口改动；点击排名下钻；排名指标切换（仅总 token）。
