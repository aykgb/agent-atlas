# IO 优化任务

2026-09-19 评审结论。基线：本机索引 2722 轮 + 远端 1371 轮，`.stats/index.sqlite` 412MB，常见搜索约 1s。六项独立可做，完成后在 `STATUS.md` 回填状态。

## 1. 搜索路径：长词去 LIKE、合并计数、只取当页全文（P0）

现状（实测）：

- `search_clauses`（stats_server.py:539）对每个词都附加 `search_text LIKE`，即使 ≥3 字已走 FTS MATCH：`q=mcp` 的 `count(*)` 610ms，只走 FTS 0.9ms。
- LIKE 不是防陈旧索引：文件级删除在重解析前执行，用的是旧文本，合成测试无残留（删后 MATCH 旧词 = 0）。
- 两者唯一差异是内嵌 NUL：7 条 turn 含 NUL 字节，LIKE 在 NUL 处截断、FTS 可见——FTS 结果更完整。
- `count(*)` 与取行各扫一遍；取行带 `LIMIT page*20` 的全文：本地 1060 行 265ms，不带全文 88ms；深页每源可取上百 MB 全文。

改动：

- `search_clauses`：仅 <3 字的词保留 LIKE，≥3 字只用 FTS MATCH。
- 计数并入取行查询：`COUNT(*) OVER ()`（实测 875ms → 337ms）。
- 合并分页只取排序所需列，最终 20 行再取 input/output 计算 `match` 片段。

验收：`q=mcp&page=1` 端到端 <50ms（含远端）；2 字查询不慢于现状；短词结果不变，长词允许多出 NUL 之后的真实命中；现有搜索契约用例全过。

## 2. 导出与远端同步：批量取词、加大页（P1）

现状（实测）：`/api/export` 每轮一次 terms 查询（stats_server.py:600），500 轮 200ms；`sync_remote` 每页 50 轮（stats_server.py:394），1371 轮 28 次往返。

改动：terms 用 `turn_id IN (...)` 一次取回；同步页大小 50 → 500。

验收：导出 500 轮 <100ms；同步 1371 轮往返 ≤3 次且数据与全量一致；相关测试的 `limit=50` 断言同步更新。

## 3. 静态资源缓存（P2）

现状：每次请求 `read_bytes()` 读盘，响应带 `Cache-Control: no-store`（stats_server.py:707、755）。

改动：按 mtime+size 生成 ETag，命中 `If-None-Match` 返回 304；`/`、`/app.js`、`/style.css` 用可协商缓存。

验收：二次请求返回 304；响应头含 ETag；页面功能不变。

## 4. 重建：FTS 批量 rebuild（P2，收益约 1s）

现状（实测）：重建 13.8s = SQLite 写入 10.9s（其中 FTS 插入约 11s）+ 读日志约 2.5s + jieba 1.4s；`journal_mode=OFF/synchronous=OFF` 无效（13.8 → 13.5s）。去掉 FTS 只建 turns 为 2.8s；官方 `INSERT INTO search(search) VALUES('rebuild')` 为 9.7s。

改动：`rebuild` 先插入全部 turns 与 search_text，最后执行一次 FTS `rebuild` 命令。

验收：重建后搜索结果与逐行插入一致；重建时间下降 ≥1s（读取时间不回归）。

## 5. 去掉 turns.search_text 重复列（P2，schema v4）

现状（实测）：`search_text` 79.8MB（本机）＋约 40MB（远端），与 input+output 完全重复，占本机 DB 412MB 的约 20%。

改动：FTS 改 contentless（`content=''` + `contentless_delete=1`），删除该列，`match` 片段由 input/output 还原；涉及 `rebuild`、`insert_turn`、`delete_file_rows`、`/api/export`、远端同步与 schema 版本 +1。

依赖：`contentless_delete` 需 SQLite ≥3.43。本机 uv venv 为 3.50.4，已验证可用；Python 3.10/3.11 自带 SQLite 较旧，需按 `sqlite_version_info` 检测并保留旧路径，或把下限提到 3.12。

验收：DB 体积下降 ≥80MB；搜索、导出、同步、增量更新用例全过；旧索引启动时自动升级（触发一次全量重建）。

## 6. 小项（P3）

- dsh 解压改流式（`Popen.stdout` 迭代），去掉整文件 `capture_output` 的内存峰值，解析结果不变。
- `records = list(read_jsonl(...))` 全量物化：最大 20MB 文件解析仅 91ms，属内存峰值而非 IO，暂不动。
- `/api/meta` 的 `count(*)` 写入 meta（12ms/次），收益小，不做。
- 追加写 offset 续读：解析成本远低于 FTS 写入，不做。

## 实测（2026-09-19 完成）

- #1：`/api/search?q=mcp` 端到端 859ms → 21ms（含远端）；2 字查询本机 264ms，与改造前逐项一致；计数并入取行（`COUNT(*) OVER ()`），合并只取排序所需列，当页 20 行再取 input/output 拼回 `match`。
- #2：导出 500 轮 65ms；同步页 500，本机 1376 轮全量重拉 3 个 turns 请求，增量二次同步 103ms。
- #3：`/`、`/app.js`、`/style.css`、`/favicon.ico` 按 mtime+size 生成 ETag，`If-None-Match` 命中返回 304（`Cache-Control: no-cache`）。
- #4+#5：contentless FTS 不允许 `rebuild`（SQLite 报错），故 #4 改为 turns 全部插入后用一条 `INSERT INTO search(rowid,search_text) SELECT …` 批量填充。旧新交替各 3 次：13.99s → 13.43s（-0.56s，未达 -1s 目标）。拆分实测：SQL 批量填充 9.79s、Python 重建文本 + executemany 9.89s，瓶颈在 trigram 索引化本身；原目标的 1s 差额来自旧 schema 的官方 `rebuild` 命令，contentless 下不可用，收益随之缩小。
- #5：turns 去掉 `search_text`，FTS 改 `content=''` + `contentless_delete=1`（schema v4，旧索引与远端库启动后自动重建/重拉）。本机索引 391.5MB → 297.6MB（-94MB），远端库 200.8MB → 150.8MB（-50MB，远端迁移分支补 VACUUM 以释放旧页）。不足 3 字的查询按 `input` 与输出原文（json_each 还原）拼接后字面扫描，`match` 片段同源拼接。
- #6：dsh 解压改 `Popen.stdout` 迭代，去掉整文件 `capture_output`；解析结果不变。
- 依赖：`contentless_delete` 需 SQLite ≥ 3.43，`pyproject.toml` 下限提到 3.12，运行时按 `sqlite_version_info` 复核并报明确错误。

## 已评估、不做

- journal/synchronous pragma：13.8 → 13.5s，噪声级。
- trigram `detail=none`：SQLite 报 parse error，不支持。
- 引入 tgrep：面向磁盘文件，与本项目 SQLite 行模型不匹配，且违反依赖约束。
