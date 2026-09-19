# agent-atlas 状态

任务台账。新特性先写 `docs/tasks/` 任务文档、在此表登记，再实现；完成把状态改为 ✅。说 “status” 时按 `skills/status/SKILL.md` 采集实况并汇报。

| # | 任务 | 优先级 | 状态 | 详情 |
|---|---|---|---|---|
| 1 | 搜索：长词去 LIKE、合并计数、只取当页全文 | P0 | ✅ | q=mcp 859→21ms；2 字查询持平 |
| 2 | 导出/远端同步：terms 批量化、页 50→500 | P1 | ✅ | 导出 500 轮 65ms；同步 3 次往返 |
| 3 | 静态资源 ETag/304 | P2 | ✅ | 二次请求 304，测试覆盖 |
| 4 | 重建：FTS 批量 rebuild | P2 | ✅ | 13.99→13.43s（-0.56s，未达 -1s）；contentless 无 rebuild，改批量 INSERT…SELECT |
| 5 | 去掉 turns.search_text 重复列（schema v4） | P2 | ✅ | 本机 -94MB、远端 -50MB；旧索引自动重建 |
| 6 | 小项：dsh 流式解压等 | P3 | ✅ | Popen.stdout 迭代，解析不变 |
| 7 | 用量柱状图双击跳转会话搜索并填参数 | P2 | ✅ | [usage-drilldown.md](docs/tasks/usage-drilldown.md)：CDP 冒烟全过 |
| 8 | 每 30 分钟自动检查本机索引（watch_index，不自动同步远端） | — | ✅ | 7ee378c |
| 9 | dsh 第六类 agent：zstd 解压、用量口径、注入消息归入「上下文」 | — | ✅ | 8c21808 |
| 10 | 远端数据独立存放 + cursor/指纹增量同步 + 启停不删数据 | — | ✅ | 0f4b287、ead14c5 |
| 11 | 刷新改增量（水位 + size）、远端面板每台独立启停 | — | ✅ | 0f4b287 |
| 12 | 控制脚本未就绪则阻塞到就绪（server.sh / server.ps1） | P2 | ✅ | [server-wait-ready.md](docs/tasks/server-wait-ready.md) |

状态：空 = 待办，🔄 = 进行中，✅ = 完成；详情列为任务文档或提交号。
