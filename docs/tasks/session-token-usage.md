# 会话列表显示每个会话的 token 消耗

来源：2026-09-21 用户需求「希望在『会话』tab 上看到一个会话的 context usage」。口径经确认为**会话总 token 消耗**（新增 + 缓存写 + 缓存读 + 输出的累计值），显示在**会话列表每行一列**，**本机与远端会话都要**。这条推翻了 [session-list.md](session-list.md) 的非目标「不在列表页展示 token 用量」。

## 现状

- `file_usage(path,agent,key,day,model,new,cc,cr,out,msgs)` 不含 `session`：`Dataset.add_usage` 生成的事件带 `session`，`store_dataset` 落库时丢掉了。按会话聚合当前查不出来。
- 用量事件与 `turns` 没有共同标识（Claude 用量键是 assistant 消息 id，轮次标识是 user 消息 uuid），也没有逐条时间戳，因此只能做会话级累计，做不了逐轮曲线。
- `refresh_usage` 把 `file_usage` 去重后聚合进 `usage(day,agent,model,…)`；跨文件重复按 `(agent,key)` 取各字段 MAX，这是既有口径，不能动。
- 远端同步只拉 `usage` 聚合表（`/api/export?section=usage` 全量替换）与 `turns` 增量；没有任何会话维度的用量。
- `/api/sessions` 按 source 对 `turns` 分组，合并后在 Python 里排序分页；远端条目受该远端的 `search` 勾选控制。

## 方案

### 索引（SCHEMA_VERSION 4 → 5）

1. `file_usage` 增加 `session TEXT`（置于 `model` 之后），`store_dataset` 的 INSERT 补上 `u['session']`。
2. 新增 `session_usage(agent,session,new,cc,cr,out,msgs)`，主键 `(agent,session)`；本机由 `refresh_usage` 物化，远端由同步写入，两侧表结构一致，查询端无需分支。
3. `refresh_usage` 把去重子查询抽成常量 `DEDUPED_USAGE` 供两次聚合复用，保证会话口径与日用量口径同源：
   - 内层不变地按 `agent,key,day,model` 分组取 MAX，额外带出 `MIN(session) AS session`；同一 `(agent,key)` 在日志里改写过 session id 时（如 codex `session_meta`）归给字典序最小的那个，确定且不重复计。
   - 外层一份按 `day,agent,model` 写 `usage`（结果与改前逐格一致），一份按 `agent,session` 写 `session_usage`。
4. schema 升版后本机索引自动全量重建（`index_version != SCHEMA_VERSION`），远端库同理自动全量重拉，无需手工操作。

### 同步协议

5. `/api/export?section=usage` 的返回值增加 `sessions`：`SELECT agent,session,new,cc,cr,out,msgs FROM session_usage ORDER BY agent,session`。
6. `sync_remote` 在同一事务内 `DELETE FROM session_usage` 后批量插入 `page.get('sessions') or []`；旧版远端（v4，不返回该字段）按空处理，远端会话显示「—」而非报错。版本不符时的 DROP 列表补上 `session_usage`。

### 查询（`/api/sessions`）

7. 每个 source 的分组查询改为 `LEFT JOIN session_usage ON agent,session`（主键唯一，不影响 `count(*)`），行内返回扁平的 `new,cc,cr,out,msgs` 与 `total`（`sum(FIELDS[:-1])`，与 `/api/usage` 字段命名一致）。
8. 该远端未勾选 `usage` 时不带用量（`total` 为 `null`），与「用量不汇入」的既有语义一致；本机始终带。
9. 排序、分页、agent 筛选与返回结构其余部分不变。

### 前端

10. `app.js` 的 `renderSessions`：在「N 轮」与来源之间插入一列 `compact(total)`，`data-tip` 给出「新增 / 缓存写 / 缓存读 / 输出 / 调用」明细，渲染后 `bindTips($('sessionList'))`；`total` 为 `null` 时显示「—」且不挂提示。
11. `style.css` 加 `.session-tokens`（右对齐、`tabular-nums`），不调整既有列。

## 验收

1. 会话 tab 每行显示该会话的 token 总量（compact 形式），悬停给出四项明细；本机与远端会话都有值，远端未勾选用量时为「—」。
2. 用量概览页数值与改前逐格一致（schema 升级只加列不改口径）：同一天同一 agent 的总量不变。
3. 抽查一个会话：列表行的总量 = 该会话各模型 `new+cc+cr+out` 之和，与 `/api/usage` 按天口径不冲突。
4. 首次启动自动重建索引（v4 → v5），远端自动全量重拉一次后会话列显示正常；对接旧版远端不报错、该远端会话显示「—」。
5. 新增契约测试：`file_usage` 带 session 后 `usage` 聚合结果不变；`session_usage` 按会话累计正确且跨文件重复只计一次；`/api/sessions` 返回用量字段并遵守远端 `usage` 勾选；`/api/export` 带 `sessions` 且 `sync_remote` 能吞下缺字段的旧版响应。`node --check static/app.js` 与既有测试全过。

## 实测（2026-09-21，本机 macOS）

- 契约测试 36 项全过（新增 `test_sessions_carry_token_totals_and_count_duplicates_once`、`test_session_totals_reach_remote_and_honor_usage_flag`），`node --check static/app.js` 通过。
- 索引 v4 → v5 自动全量重建，`./server.sh restart` 16.4s 就绪；`/api/sessions` 首页 50 行全部带用量，例：opencode `ses_f3d340991ffep52BSBAz` 12 轮 total=46,508,617（new 367,164 / cr 45,932,416 / out 209,037 / msgs 210）。
- 远端库落后一个 schema 版本时按「待同步」处理：查询里排除（会话数 790 → 470），`/api/remotes` 给出 `索引格式已升级，请重新同步`；`POST /api/sync` 16.4s 全量重拉 1396 轮后恢复 790。
- 对接旧版远端（127.0.0.1:28763 仍为 v4，`/api/export` 不返回 `sessions`）：同步无错误，该远端 10 个会话用量为 `null`（前端显示「—」），本机行不受影响。与 v5 远端互通的路径由契约测试覆盖（远端会话 total=15，取消勾选用量后为 `null`）。
- CDP 冒烟：列表 790 会话 / 16 页，token 列显示 `claude 2.95M`、`opencode 46.51M`；悬停给出「新增 84 / 缓存写 81,387 / 缓存读 2,843,883 / 输出 24,384 / 调用 42」，移开即隐藏；旧版远端行显示「—」且不挂提示。会话详情、返回、翻页与用量 tab 均无回归，无控制台报错。

## 非目标

- 不做逐轮上下文曲线、不做上下文窗口占用百分比（需要 model → 窗口大小对照表，本机 40+ 个本地模型无从得知）。
- 不做按用量排序会话列表、不加筛选项。
- 不改 `turns` schema 与轮次增量同步协议（cursor / 指纹逻辑原样）。
