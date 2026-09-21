# CLI 改为纯客户端：stats-today.py 从 server /api/usage 读取

来源：2026-09-20 用户需求。`stats-today.py` 从「直接读日志」改为「调用本机 server 的 `/api/usage`」，从而纳入启用中的远端数据。

## 现状与落点

- 现状：`stats-today.py` 调 `stats_data.collect(home)` 直读原始日志（仅本机，不含远端），按 `(day,agent,model)` 聚合。
- 落点：改为 GET 本机 server 的 `/api/usage`（已合并本机 + 启用远端），CLI 只做参数映射与聚合展示。
- `/api/usage` 现状只支持 `unit`+`n`（区间结束于今天），无法表达「某个过去的单日」；`bounds()` 已有 `start`/`end` 管道，但被 `period()` 计算值覆盖。

## 口径

- 数据源：server 的 `/api/usage`（本机 + 启用远端，按 `(day,agent,model)` 求和），本机数字口径不变。
- 参数映射：`date=X` → `start=X&end=X`；`--week` → `n=7`；`--days N` → `n=N`；默认 → `n=1`（今天）。`unit` 恒为 `day`。
- 输出：与现 CLI 一致（agent×模型明细 + 多日每日各 agent 总量）；`--json` 保留 `start/end/usage`，去掉 `warnings`（纯客户端不再产生解析告警，需要时查 `/api/meta`）。
- server 未启动：非零退出并提示。

## 实现要点

- `stats_server.py`：把 `period()` 的 bucket 循环抽成 `buckets_between(start, end, unit)` 复用；`/api/usage` 在传入显式 `start`/`end` 时优先用之（`end` 缺省取 `start`，自动纠正 start>end），否则走 `period(unit, n)`。`period()` 行为与返回值不变。
- `stats-today.py`：删除 `collect()` 与 `--home`；新增 `--url`（默认 `http://127.0.0.1:18763`）；`urllib.request` GET `/api/usage`；按 API 返回的 `start`/`end` 推导日期区间与 `n`；聚合逻辑与输出格式不变。
- 前端不受影响（`app.js` 从不用 `start`/`end` 调 `/api/usage`）。

## 验收

1. `stats-today.py`（无参）= 今天；`--week`/`--days N` 与旧口径一致（本机无远端时数字逐格相同）。
2. `stats-today.py 2026-09-01` 只出该单日；`date` 与 `--week`/`--days` 互斥报错不变。
3. 配置远端后，CLI 输出含远端用量（按 `(day,agent,model)` 并入）。
4. server 未启动时非零退出并提示。
5. `node --check` 与 `unittest` 全过；新增 `/api/usage` 显式 `start`/`end` 的契约测试。

## 非目标

- 不改 `stats_data` 解析/去重口径；不改前端；不做 per-source（本机/远端）拆分展示；`--json` 不保留 `warnings`。
