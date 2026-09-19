# 用量柱状图双击下钻到会话搜索

来源：2026-09-19 用户需求。在「用量概览」的堆叠柱状图上双击某段，跳到「会话搜索」并自动填好对应参数。

## 现状与落点

- 柱段渲染：`static/app.js:166-179`，每段是 `<rect tabindex="0" data-tip="…">`，目前只带提示文本，没有 bucket 与分组信息。
- 分组键：`keyOf`（static/app.js:140）= `grouping`（`agent` / `model` / `pair`）+ 组合键，前 7 名之外的段为「其他」。
- 搜索参数：`filters()` 提供共用起止日期与 agent/model（static/app.js:263）；`q`、`term`、`minlen`、`maxlen` 单独控制。
- 跳转范例：词频点击（static/app.js:233-235）设置 `state.term` 后 `setTab('search')`。

## 交互与参数口径

1. 触发：双击柱段 `rect`（事件委托到 `#bars`，用 `event.target.closest('rect')`）；点到空白不响应。
2. 日期：由桶键换算 `start`/`end`（`<input type="date">` 接受 `YYYY-MM-DD`）：
   - `day`：start = end = bucket；
   - `week`：start = 周一，end = 周一 + 6 天；
   - `month`：start = 当月 1 日，end = 当月最后一天。
   - 统一用 JS `Date` 计算，不依赖后端补桶。
3. 过滤：`grouping=agent` 只设 agent；`model` 只设 model；`pair` 同时设两者；「其他」段无法用单值表达，agent/model 清空、只按日期过滤。
4. 清理与复位：`q` 清空、`state.term` 清空、`state.page = 1`；`minLen`/`maxLen` 保持原值。
5. 反馈（可选）：搜索标题旁加一行「来自用量：<bucket> · <分组>」，跳转来源可感知。

## 实现要点

- `renderCharts` 的 rect 增加 `data-bucket`、`data-key`、`data-group`（写进字符串模板，注意 `esc()`）。
- 新增一个 `drilldown(bucket, key, group)`：算日期 → 写 `#start`/`#end`/`#agent`/`#model` → `setTab('search')`（`setTab` 会触发 `load()`，无需再手动刷新）。
- 键盘可达：rect 已有 `tabindex="0"`，可加 `Enter` 触发（与 `bindTips` 的 focus 行为一致），可选。
- 远端只读页面不受影响（搜索对远端开放）。

## 验收（CDP 冒烟）

1. `unit` 分别取 day / week / month，双击不同柱段：搜索页 start/end 与桶边界一致（周末日、月末日正确）。
2. `group` 三种取值各双击一段：agent/model 选择器与段一致；pair 两值都设。
3. 「其他」段：agent/model 均为空，仅日期过滤。
4. 跳转后 q 与倒排词筛选为空、页码为 1；抽查一条结果的时间落在桶内。
5. 双击空白区域无跳转；双击后提示浮层不残留。

## 实测（2026-09-19，CDP 冒烟）

- day/agent：start=end=桶；week/pair：桶为周一、end=+6 天、组合键正确拆成 agent 与 model；month/model：7 月 → 07-01…07-31、9 月 → 09-01…09-30。
- 「其他」段清空 agent/model、仅按日期过滤；跳转后 q、词频筛选、页码复位，长度筛选保留。
- 搜索结果时间戳均落在桶内；双击空白不跳转；跳转后提示浮层不残留。

## 非目标

- 环形饼图扇区、图例与表格行的下钻；柱段单击（与悬浮提示/焦点冲突）；跨页参数记忆。
