# 会话列表与完整会话查看

来源：2026-09-19 用户需求。两个问题：如何查看一个 session 的全部输入输出；搜索结果能否跳转到完整 session。方案：新增「会话」tab，按创建时间（最新在前，用户确认）列出所有 session，点开即完整会话；搜索结果每个轮次加「完整会话」入口跳转到同一视图。

## 现状

- 数据层：`turns` 表已有 `agent`、`session`、`timestamp`，但没有会话维度的接口；`/api/search` 按轮次分页，`/api/turn` 取单轮详情。
- 前端：三个 tab（用量概览 / 会话搜索 / 输入词频）；搜索结果每条轮次可展开，但无法沿 session 继续看上下文。
- 会话标识跨来源：本机 `local`，远端 `<key>`（`remote_key(remote)` 前 8 位）；`turns.id` 已是 `<key>:<id>` 形式，`/api/turn` 按前缀路由。

## 方案

### 后端（stats_server.py `query()`）

1. 新增 `GET /api/sessions`，参数：`agent`（可选）、`page`（默认 1，每页 50）。
   - 每个 source 内：`SELECT agent,session,count(*) AS turns,min(timestamp) AS first,max(timestamp) AS last FROM turns GROUP BY agent,session`，应用 agent 筛选。
   - 合并各 source（同一 agent+session 在不同来源视为不同条目），条目带 `key`、`label`（本机/远端主机）。
   - 按 `first` 降序、`key`、`agent`、`session` 升序稳定排序后分页；返回 `{rows,total,page,pages}`。
   - 不套用日期与模型筛选：列表口径是「该 session 的全部轮次」；只共用 agent 筛选。
2. 新增 `GET /api/session`，参数：`key`（默认 `local`）、`agent`、`session`。
   - 路由到对应 source；不存在时报 `会话不存在，请刷新会话列表`。
   - 返回 `{key,label,agent,session,first,last,source,turns:[{id,model,timestamp,preview}]}`，`preview=substr(input,1,240)`，按 `timestamp,id` 升序（阅读顺序）；`id` 带来源前缀（`<key>:<id>`），复用 `/api/turn` 取全文。
   - 全文不在列表接口返回，展开时仍走 `/api/turn` 懒加载（沿用现有「Agent 响应」折叠）。

### 前端（static/）

3. `index.html`：nav 增加「会话」按钮；新增 `#sessions` section：
   - 列表视图：`#sessionList`（行：agent 徽标、session 标识、轮次数、创建时间、来源），`#sessionCount`、分页按钮。
   - 详情视图：`#sessionDetail`（头部：agent、session、轮次数、时间范围、来源；返回按钮；轮次卡片容器 `#sessionTurns`）。
   - 两个视图互斥显示，详情在上、返回回列表。
4. `app.js`：
   - `state` 增加 `sessionPage`、`sessionDetail`。
   - `setTab('sessions')`：显示 `#sessions`，隐藏其余；`periodControls`/`dateControls` 在会话 tab 均隐藏（只保留 agent 筛选）；`load()` 按 tab 分发到 `loadSessions()`。
   - `loadSessions()`：调 `/api/sessions`，渲染列表；点击行调 `openSession({key,agent,session})`。
   - `openSession(target)`：调 `/api/session` 渲染详情；轮次卡片复用 `renderSearch` 的卡片结构（`<details class="turn" data-id>` + `loadTurn`），时间升序，默认收起，`loadTurn` 不变。
   - 搜索结果卡片（`renderSearch`）每条加「完整会话」按钮：由 `r.id` 前缀取 key，携 `r.agent`、`r.session` 切到会话 tab 并 `openSession`；详情头部同样有返回列表。
   - 会话列表为空时给空态文案。
5. `style.css`：会话列表行与详情头部样式（复用 `.turn`、`.badge`、`.source`，新增少量 `.session-row`、`.session-head`）。

## 验收

1. 「会话」tab 列出全部 session：最新创建的在前；同 agent 多来源各自成行；分页 50/页可翻页；agent 筛选生效，日期/模型筛选不出现。
2. 点击任一 session：显示该 session 全部轮次（时间升序，序号与轮次数一致），展开单轮得到完整输入与「Agent 响应」，内容与搜索结果展开一致。
3. 搜索结果任一卡片的「完整会话」：切到会话 tab 并打开对应 session；远端轮次（`<key>:<id>`）路由到对应远端库，头部来源显示远端标签。
4. 详情返回列表后筛选与页码保持。
5. `unittest` 新增契约：`/api/sessions` 分组计数/排序/分页与跨来源合并；`/api/session` 路由（本机与远端前缀）、轮次升序、`preview` 截断；不存在时 `ValueError`。`node --check static/app.js`、现有测试全过。

## 实测（2026-09-19，本机 macOS）

- `unittest` 31 项全过（新增 `test_sessions_list_groups_orders_pages_and_filters`、`test_session_detail_routes_remote_and_honors_search_flag`），`node --check static/app.js` 通过。
- CDP 冒烟：会话 tab 列出 750 个会话、15 页、最新在前（18:41 → 18:06 → …），本机与远端各自成行并带来源标签；打开 opencode 会话（2 轮）时间升序、展开单轮得到「用户输入 / Agent 响应 · 125 段」；返回列表、翻页（1/15 ↔ 2/15）正常。
- 搜索结果「完整会话」：本机 `local:3106`（pi）与远端 `3cb5d5b0:1378` 均正确跳到会话详情，远端头部来源为 `127.0.0.1:28763 · …`。
- agent 筛选 opencode → 117 个会话、3 页，行内 agent 全为 opencode；会话 tab 下模型与周期/日期筛选隐藏，其余 tab 恢复。
- 无控制台报错。

## 非目标

- 不做会话内搜索、导出、重命名；不解析 Markdown。
- 不在列表页展示 token 用量（用量按 day/model/file 记录，与会话无直接键）。
- 不引入前端路由（刷新不保持会话位置）；不改 `turns` schema 与同步协议。
