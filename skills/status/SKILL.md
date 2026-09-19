---
name: status
description: 汇总 agent-atlas 项目状态：读取 STATUS.md 任务面板，采集服务、索引、远端同步、git 工作区实况，报任务与下一步。当用户说 "status"、"项目状态"、"任务状态"、"进度"、"接下来做什么" 时使用。
---

# status — 项目状态速报

只读采集，不改索引与日志；本 skill 只写 `STATUS.md` 一处（仅在第 4 步）。

1. 读任务台账：`STATUS.md` 的单张表格（状态空 = 待办，🔄 = 进行中，✅ = 完成）；未完成任务再打开它链接的 `docs/tasks/*.md`。
   完成标准：能列出未完成任务与最近完成各是什么。
2. 采集实况（可并行）：
   - `./server.sh status`
   - `curl -s http://127.0.0.1:18763/api/meta`
   - `curl -s http://127.0.0.1:18763/api/remotes`
   - `git status --short` 与 `git log --oneline -5`
   完成标准：服务 PID、索引 turns/updated/warnings、每台远端 enabled 与 `status` 里 turns/synced_at/error、未提交改动，都有确切值。服务未启动时记录为停止，并给出 `./server.sh start`，由用户决定是否启动。
3. 汇报（≤10 行，只列有内容的项）：
   - 台账：未完成（进行中在前）→ 最近完成；
   - 实况：服务、索引（轮数 / 更新时间 / 告警）、远端同步（失败要带 error）、git 工作区；
   - 下一步：从未完成任务里挑 1–2 条，没有就说无。
4. 用户说完成时把 `STATUS.md` 对应行的状态改为 ✅，在详情列补一行结果（提交号或结论）；不新增区块，细节留在 `docs/tasks/`。
