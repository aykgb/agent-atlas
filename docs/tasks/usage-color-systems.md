# 用量概览：固定 Agent 与模型标志性品牌色并区分色彩体系

来源：2026-11-17 用户需求。在「用量概览」中固定每个 Agent 和每个模型的专属颜色，选取最具代表性的标志性品牌色（如 GPT 黑色、Claude 橘色、DeepSeek 蓝色），采用克制、现代的高级配色风格，并与「Token 构成分布」的色彩体系区分开来。

## 现状与问题

1. **三类维度共用同一组颜色**：原 `COMP_PARTS` 与 `SERIES_COLORS` 前 4 项完全相同，顶部的「Token 构成分布」与下方的「Agent / 模型趋势与占比」撞色。
2. **颜色未锚定品牌认知且随排名跳变**：趋势图与占比图按当前筛选/口径下的排名序号 `0..6` 取色，切换时间或口径后同一 Agent / 模型颜色改变，且缺乏厂商标志性视觉认知。
3. **排名榜与明细表缺少对应色标**：「Agent 使用排名」与「模型使用排名」统一使用单色 `var(--accent)`。

## 方案设计

### 1. 标志性品牌色阶与雾面结构色分离（`static/app.js`、`static/style.css`）
- **Token 构成分布（`COMP_PARTS`，低饱和雾面建筑矿物色系）**：
  - `new` 新增输入：`#3898bd`（冷钢天青）
  - `cc` 缓存写入：`#c99b49`（香槟砂金）
  - `cr` 缓存读取：`var(--comp-cr)`（浅色 `#7c93a8` 雾面青岩灰 / 深色 `#556d82`，占 90%+ 面积时内敛不抢戏）
  - `out` 模型输出：`#9e5b8a`（烟熏暮山紫）
- **模型家族标志性品牌色阶（`MODEL_FAMILY_RULES` + `MODEL_FAMILY_SCALES`）**：
  - 按模型名称识别所属厂商/家族，同家族首个模型取纯正品牌主色，同屏多个同家族变体按明度/色相微调阶梯展开并固化到 `state.modelColors`：
    - **GPT / OpenAI 系列（`gpt-*`、`o1/o3/o4`、`codex/*`）**：**标志性曜石炭黑阶梯** `var(--c-gpt-1..4)`（浅色 `#181c23` → `#303744` → `#475163` → `#606c80`；深色自适应为黑钛铂银 `#d4dce8` → `#a6b4c8` → `#7d8da4` → `#5d6c82`）。
    - **Claude / Anthropic 系列（`claude-*`、`opus/sonnet/haiku`）**：**标志性陶土暖橘阶梯**（`#d96b43` → `#e6845a` → `#bd522c` → `#eea077`）。
    - **DeepSeek 系列（`deepseek*`、`dsv*`）**：**标志性深海鲸蓝阶梯**（`#2b5aed` → `#4e79f6` → `#1e43bd` → `#7598fa`）。
    - **Qwen / 通义千问系列（`qwen*`、`qwopus*`）**：**标志性千问典雅紫阶梯**（`#6c47d6` → `#8764e6` → `#5230b5` → `#a183f0` → `#3e2291`）。
    - **Gemini / Google 系列（`gemini-*`）**：**标志性极光翡翠青阶梯**（`#0d9488` → `#26b0a3` → `#0b7369` → `#4ec7bb`）。
    - **Kimi 系列（`kimi*`）**：勃艮第玫瑰红阶梯（`#c23b66` → `#dc5a83` → `#9e2b50`）。
    - **GLM 系列（`glm*`）**：森系松石翠绿阶梯（`#298a53` → `#42a66d` → `#1e6b3f`）。
    - **Grok 系列（`grok*`）**：暗夜绯红阶梯（`#b3363e` → `#cf5159` → `#8f262d`）。
    - **Nemotron 系列（`nemotron*`）**：英伟达青柠绿阶梯（`#65a30d` → `#84cc16` → `#4d7c0f`）。
    - **其他模型（`other`）**：现代莫兰迪专业色阶（`#b07d2b`、`#3e7496`、`#8c4a7e`、`#5c873a`、`#9e5a52`、`#4f6680`）；`unknown` / `<synthetic>` 固定为 `#7c8ba1`。
- **八类 Agent 标志性专属色（`AGENT_COLORS`）**：
  - `codex`：`var(--c-gpt-1)`（曜石炭黑，呼应 OpenAI/GPT）
  - `claude`：`#d96b43`（Anthropic 陶土暖橘）
  - `dsh`：`#2b5aed`（DeepSeek 深海鲸蓝）
  - `agy`：`#0d9488`（Google Gemini 极光翡翠青）
  - `pi`：`#6c47d6`（典雅皇家紫）
  - `omp`：`#9370eb`（鸢尾浅紫，与 `pi` 同属紫色系）
  - `opencode`：`var(--c-opencode)`（高级钛灰，浅色 `#727985` / 深色 `#8b93a1`）
  - `grok`：`#b3363e`（暗夜绯红）

### 2. 用量概览全组件联动
- **用量趋势 & 用量占比**：按当前分组（`agent` / `model` / `pair` / `composition`）调用对应取色函数，展开「其他」明细同步展示各项固定色块。
- **Agent 使用排名 & 模型使用排名**：名称前展示固定色块 `.swatch`，进度条 `.rank-fill` 使用该 Agent 或模型的固定品牌色。
- **Agent × 模型明细表**：首列模型名与 Agent 名旁展示各自对应的固定色块。

## 验收与实测（2026-11-17）

- `node --check static/app.js` 与 `unittest`（69 项契约测试）全部通过。
- Chrome DevTools 实测：
  - Token 构成分布呈现低饱和雾面青岩灰（`#7c93a8`）主调与冷钢天青/香槟砂金/烟熏暮山紫点缀，与下方品牌色体系层次分明。
  - GPT（`gpt-6.1-sol`）为曜石黑 `rgb(24, 28, 35)`，Claude（`claude-opus-5-5`）为陶土橘 `rgb(217, 107, 67)`，DeepSeek（`deepseek-v4.1-flash`、`opencode-go/deepseek-v4.1-flash`、`deepseek-v4.1-flash-free`）为深海鲸蓝同色系阶梯，Qwen 为典雅紫阶梯，Gemini 为极光青阶梯；切换口径或筛选后各 Agent 与模型颜色恒定不变。
