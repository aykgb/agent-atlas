# 远端面板：显示同步时间

来源：2026-09-23 用户需求。远端面板状态列只有「N 轮已同步 / 同步失败 / 未同步」，看不出远端数据有多新鲜。

## 现状与落点

- 后端 `remote_status`（stats_server.py:134-150）已返回每台远端的 `synced_at`：成功同步时写入远端库 meta（stats_server.py:450-453），失败只写 `sync_error`，`synced_at` 保留上次成功值；`/api/remotes` 原样透出。后端无需改动。
- 前端 `renderRemotes`（static/app.js:53-75）：状态列无时间。
- 页面已有时间格式：`new Date(...).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})`（app.js:134「更新于」）。
- 手动同步后 `syncRemotes → metadata → loadRemotes → renderRemotes` 会重新拉取并渲染，新时间自然刷新。

## 方案

- 前端加 `syncTime(iso)` 助手（与页脚「更新于」同格式）。
- `renderRemotes` 的「N 轮已同步」状态追加 `· MM/DD HH:mm`（仅 `synced_at` 存在时）。
- 同步失败行不显示时间：裸时间跟在「同步失败」后有歧义（是失败时刻还是上次成功？），错误详情仍在 title。

## 验收

1. 成功同步后，远端行显示「N 轮已同步 · MM/DD HH:mm」。
2. 未同步 / 已停用 / 同步失败三行不变。
3. `unittest` + `node --check` 通过。

## 非目标

- 不改后端接口与 meta 存储。
- 不在同步失败行显示时间、不加「上次成功」标签。
