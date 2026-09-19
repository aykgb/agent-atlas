# agent-atlas 状态

任务台账。新特性先写 `docs/tasks/` 任务文档、在此表登记，再实现；完成把状态改为 ✅。说 “status” 时按 `skills/status/SKILL.md` 采集实况并汇报。

| # | 任务 | 优先级 | 状态 | 详情 |
| --- | --- | --- | --- | --- |
| 1 | 搜索：长词去 LIKE、合并计数、只取当页全文 | P0 | ✅ | [io-optimizations.md](docs/tasks/io-optimizations.md) §1：q=mcp 859→21ms；2 字查询持平 |
| 2 | 导出/远端同步：terms 批量化、页 50→500 | P1 | ✅ | [io-optimizations.md](docs/tasks/io-optimizations.md) §2：导出 500 轮 65ms；同步 3 次往返 |
| 3 | 静态资源 ETag/304 | P2 | ✅ | [io-optimizations.md](docs/tasks/io-optimizations.md) §3：二次请求 304，测试覆盖 |
| 4 | 重建：FTS 批量 rebuild | P2 | ✅ | [io-optimizations.md](docs/tasks/io-optimizations.md) §4：13.99→13.43s（-0.56s，未达 -1s）；contentless 无 rebuild，改批量 INSERT…SELECT |
| 5 | 去掉 turns.search_text 重复列（schema v4） | P2 | ✅ | [io-optimizations.md](docs/tasks/io-optimizations.md) §5：本机 -94MB、远端 -50MB；旧索引自动重建 |
| 6 | 小项：dsh 流式解压等 | P3 | ✅ | [io-optimizations.md](docs/tasks/io-optimizations.md) §6：Popen.stdout 迭代，解析不变 |
| 7 | 用量柱状图双击跳转会话搜索并填参数 | P2 | ✅ | [usage-drilldown.md](docs/tasks/usage-drilldown.md)：CDP 冒烟全过 |
| 8 | 每 30 分钟自动检查本机索引（watch_index，不自动同步远端） | — | ✅ | 7ee378c |
| 9 | dsh 第六类 agent：zstd 解压、用量口径、注入消息归入「上下文」 | — | ✅ | 8c21808 |
| 10 | 远端数据独立存放 + cursor/指纹增量同步 + 启停不删数据 | — | ✅ | 0f4b287、ead14c5 |
| 11 | 刷新改增量（水位 + size）、远端面板每台独立启停 | — | ✅ | 0f4b287 |
| 12 | 控制脚本未就绪则阻塞到就绪（server.sh / server.ps1） | P2 | ✅ | [server-wait-ready.md](docs/tasks/server-wait-ready.md) |
| 13 | 用量概览：Agent 与模型使用排名 | P2 | ✅ | [usage-rankings.md](docs/tasks/usage-rankings.md)：CDP 冒烟通过 |
| 14 | 已打开页面自动更新（轮询 meta + 自动检查按挂钟计时） | P1 | ✅ | [auto-refresh-page.md](docs/tasks/auto-refresh-page.md)：CDP 冒烟通过 |
| 15 | 会话详情：Agent 响应统一折叠、段落按原顺序排列 | P1 | ✅ | [turn-output-grouping.md](docs/tasks/turn-output-grouping.md)：CDP 冒烟通过 |
| 16 | 会话列表 tab + 完整会话查看 + 搜索结果跳转 | P1 | ✅ | [session-list.md](docs/tasks/session-list.md)：CDP 冒烟通过 |
| 17 | 词频支持倒序排列（低→高返回频次最低的 N 词） | P2 | ✅ | [words-ascending-order.md](docs/tasks/words-ascending-order.md)：500a1af（契约与实测通过） |
| 18 | 词频只看中文视图过滤开关（隐藏数字与英文） | P2 | ✅ | [words-hide-filters.md](docs/tasks/words-hide-filters.md)：契约33项+node检查通过，实测英文榜首切中文 |

状态：空 = 待办，🔄 = 进行中，✅ = 完成；详情列为任务文档或提交号。
