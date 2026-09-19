# 会话详情：Agent 响应统一折叠、段落按原顺序排列

来源：2026-09-19 用户需求。当前展开一轮会话后，输出按类别分成「思考 · N 段」「工具调用 · N 段」「工具结果 · N 段」「正文 · N 段」四个折叠块，每块内部再按段折叠，段落被分类打散，看不出原始顺序。

## 现状

- `loadTurn`（static/app.js）：把 `turn.output` 按 `kind` 归类，各类别一个 `<details>`，内部每段一个 `<details>`，序号是输出数组下标（全局顺序号）。
- `stats_data.py` 输出段的 `kind` 取值：`上下文`、`思考`、`工具调用`、`工具结果`、`正文`、`过程`（Codex commentary）。dsh 的注入消息是 `上下文`。
- 段落文本为纯文本，前端 `highlight()` 做关键词高亮，不解析 Markdown。

## 方案

`loadTurn` 改为两层折叠：

1. 第一层「用户输入」（现有）与「Agent 响应 · N 段」（`N = turn.output.length`，无输出时不渲染）。
2. 「Agent 响应」内部按 `turn.output` 原顺序逐段渲染，每段一个折叠块：标题为「第 N 段 · X 字符」+ 类别标签（`thinking` / `tool call` / `tool result` / `text` / `context` / `process`），正文仍是 `<pre>` 纯文本 + 高亮。
3. 类别标签固定英文小写、等宽字体样式，用 `kind` 映射：思考→thinking、工具调用→tool call、工具结果→tool result、正文→text、上下文→context、过程→process。
4. 「Agent 响应」整体缩进（左内边距 + 左侧竖线），段落块再缩进一级，形成两级层次。
5. 段落序号 = 输出数组下标（保持既有口径：保留原顺序，不因类别重排）。

## 验收

1. 展开一轮：只见「用户输入」与「Agent 响应 · N 段」两个顶层折叠块；N 等于该轮可见输出段数。
2. 展开「Agent 响应」：段落按原顺序出现，序号连续；每段有对应英文标签，标签与 `kind` 一致。
3. 无输出轮次显示「该轮尚无可见输出。」；请求失败仍可收起重试。
4. 缩进可见：「Agent 响应」内容相对顶层缩进，段落相对响应缩进。
5. `node --check static/app.js`、`unittest` 通过；CDP 冒烟（展开 opencode 轮次，检查标签与顺序）。

## 实测（2026-09-19，本机 macOS）

- `unittest` 29 项全过，`node --check static/app.js` 通过。
- CDP 冒烟：搜索「do fix」展开三轮。opencode `local:3477`（67 段）与 `local:3476`（125 段）：顶层只有「用户输入」与「Agent 响应 · N 段」；响应内段落序号连续（第 1…N 段）、按原顺序，标签集合 {thinking, tool call, tool result, text} 与 kind 一一对应；响应体缩进 16px、段落再缩进 10px；截图确认两层缩进与右侧标签样式；无控制台报错。

## 非目标

- 后端 `stats_data.py` 的 kind 取值与段落切分不变。
- 不解析 Markdown、不做语法高亮；不改变搜索、词频与用量口径。
