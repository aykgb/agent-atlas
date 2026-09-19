# 用量概览：Agent 与模型使用排名

来源：2026-09-19 用户需求。在「用量概览」增加两个排名：agent 使用排名、模型使用排名。

## 现状与落点

- `renderUsage`（static/app.js）已把 `data.rows` 按 agent+模型汇总成 `rows` Map，并算出各指标合计与总量。
- 落点在「用量趋势 / 用量占比」之后、「Agent × 模型明细」之前：新增 `.rank-grid` 两栏并排，窄屏单列。

## 口径

- 与用量页同源：共享 agent / 模型筛选与最近 N 个周期；排名指标为总 token（新增输入 + 缓存写入 + 缓存读取 + 输出）。
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

## 非目标

- 后端接口改动；点击排名下钻；排名指标切换（仅总 token）。
