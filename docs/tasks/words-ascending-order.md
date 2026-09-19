# 词频：支持倒序排列（低→高）

来源：2026-09-19 用户需求。词频列表当前固定按次数高→低展示，需求是支持倒序（低→高）。

## 现状与落点

- 后端 `/api/words`（stats_server.py:660-683）：合并本机与远端后按 `(-count, term)` 排序，返回前 `limit`（20/40/100）个词。
- 前端 `renderWords`（static/app.js:287）：按 API 返回顺序渲染，条形宽度按显示集内最大值归一；index.html「展示前 N 词」选择器只控制数量。

## 方案

- 后端：`/api/words` 增加 `order` 参数（`desc` 默认 / `asc`）。`asc` 时按 `(count, term)` 排序取前 `limit` 项——即返回频次最低的 N 个词，而非把前 N 个词反过来。
- 前端：「展示前 N 词」旁新增方向切换（高→低 / 低→高），向 API 传 `order`；升序时数量选择器文案改为「末 N 词」。条形宽度仍按显示集内最大值归一。
- 远端合并：`merged` 字典含全部词，`asc` 对合并数据同样成立，无需额外处理。

## 口径

- `asc` 返回全集中频次最低的 N 个词，列表从低到高；同次数按 term 升序。
- 默认 `desc` 行为不变：次数最高前 N，同次数按 term 升序。
- 词排除、点击词语跳转会话搜索等现有功能不受影响。

## 验收

1. 默认（高→低）展示与现状一致。
2. 切到低→高：返回频次最低的 N 个词（非前 N 反转），列表从低到高排列。
3. 远端合并场景：升序对合并数据同样成立。
4. 契约测试覆盖 `order` 参数（默认行为、升序取最低、同次数按 term 排序）；`unittest` + `node --check` 通过。

## 实测

- 契约测试：新增 `test_words_order_ascending_and_descending`，覆盖默认行为（desc）、显式 desc、asc 取最低频次 N 词、同频次按 term 升序、非法 order 报错（400），并在 `test_query_merges_local_and_enabled_remote_sources` 验证远端合并场景；运行 `unittest`（32 项）全过。
- 语法检查：`node --check static/app.js` 通过。
- 接口实测：
  - `GET /api/words?limit=5` 返回最高前 5 词（pi 109, 提交 78, admin 72...）；
  - `GET /api/words?limit=5&order=asc` 返回频次最低 5 词（均为 count=1，按 term 升序）；
  - `GET /api/words?order=invalid` 返回 400 `{"error": "order 须为 desc 或 asc"}`。
- 前端实测：方向切换联动更新选择器文案（「前 N 词」↔「末 N 词」），条形图按当前集合内最大值归一渲染。

## 非目标

- 不改 `limit` 语义（仍是显示列表的词数）。
- 不新增其它排序键（仅按次数）。
- 不做前端本地排序替代后端参数。
