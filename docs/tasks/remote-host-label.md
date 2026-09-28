# 远端面板：给远端主机打标签（备注名）

来源：2026-09-28 用户需求。远端行只显示 `主机:端口`，多台设备（尤其 Tailscale 地址）难以分辨谁是谁。经确认取「单个备注名」形态，不是多标签。

## 现状与落点

- `remotes.json` 每项字段：`host`、`port`、`enabled`、`usage`、`search`、`words`（stats_server.py:66-92 `clean_remotes` 校验并归一化，91 行拼装）。
- 面板行由 `renderRemotes`（static/app.js:54-84）拼装；已有「启用」复选框走同一套「改动即 `saveRemotes` 整表提交」的模式，可直接复用。
- 备注名只影响展示，不参与 `remote_key`（sha1(host:port)）、不影响落盘文件名与轮次 id，因此无迁移。

## 方案

- `clean_remotes` 增加 `label`：`str(entry.get('label') or '').strip()`，截断 60 字符；缺省为空串。因此可直接显示为 `r.label`，无需前端判断。
- 面板每行在「启用」后加一个 `.remote-label` 文本框（placeholder「备注名」）：`change`（失焦或回车 `blur` 触发）时以整表 `saveRemotes` 提交，非空则只改该行 `label`。
- 输入框定宽 120px，保证复制布局稳定；`同步`/`移除` 按钮组保持右对齐（见 remote-row-actions.md）。

## 验收

1. 面板可给任意远端填写/清空备注名，刷新页面后保留（写入 `remotes.json` 的 `label`）。
2. 备注名超过 60 字符被截断；缺省写法下 `label` 为空串，行为与加字段前一致。
3. `unittest` + `node --check` 通过。

## 实测

- `node --check static/app.js` 通过；`unittest` 44 项 43 通过（唯一失败 `test_index_suffix_keys_by_resolved_home` 为既有环境问题，与本改动无关，未改动树上同样失败）。
- 契约测试覆盖：`label=' 书房 Mac '` 归一化为 `'书房 Mac'`、缺省为 `''`、非字符串 `7` 归一化为 `'7'`、`save_remotes` 落盘后再 `load_remotes` 一致。
- 无头 Chrome 截图（1200×300）：备注名列定宽、地址仍为可点链接、`同步`/`移除` 右对齐不变。

## 非目标

- 不做多标签、不做按标签筛选/分组统计。
- 不用备注名替换/覆盖地址显示（地址仍需可点击直达远端页面）。
- 不改 `remote_key`、不迁移已同步的远端库文件。
