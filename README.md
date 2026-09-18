# ecom-details-image · 本地复刻实测（13 行 / 52 张）

把 [`liangdabiao/ecom-details-image`](https://github.com/liangdabiao/ecom-details-image) 这套电商视觉 Skill 的 **7 步流程搬到本地跑通**，并把完整过程与产物开源出来：
**3 个看图写 Prompt + 4 个出图**，图像模型与 SkillBot 完全相同（`openai/gpt-image-2`），走 CogFoundry 路由，**13 行并行、52 张图、0 失败、6 分 8 秒**。

| | |
|---|---|
| 完整报告 | [`REPORT.md`](REPORT.md) |
| 只放本批结果的总览 | [`contact-sheet-local-only.png`](contact-sheet-local-only.png)（4 个步骤各一块 × 13 行，推荐先看这张） |
| 13 行 × 4 图总览 | [`contact-sheet-local.png`](contact-sheet-local.png) |
| 与平台 SkillBot 并排 | [`contact-sheet-vs-skillbot.png`](contact-sheet-vs-skillbot.png)（同位置上下对照） |

---

## 这是什么

原项目是一个 Claude Code / Codex 用的电商视觉 Skill：给它一张产品图 + 一句需求，它写 Prompt 并调 GPT-Image-2 出图。
本仓库做的是**把它拆成可独立运行的一条本地流水线**，用真实的模型调用跑 13 行，并把每一步的原始记录留档：

```
每行输入（brief + 参考图）
  ├─ stp_prmhero   看图写「主图 Prompt」     ──┐
  ├─ stp_prmdetl   看图写「详情图 Prompt」   ──┼─ 文本步骤（看图）
  └─ stp_prmbnner  看图写「Banner Prompt」   ──┘
        ↓
  ├─ stp_t2imain   文生图样张（不看参考图）
  ├─ stp_i2imain   主图（带参考图）
  ├─ stp_i2idetl   详情图（带参考图）
  └─ stp_i2ibnner  Banner（带参考图）
```

| 步骤 | 类型 | 尺寸 | 依赖 |
|---|---|---|---|
| `stp_prmhero` / `stp_prmdetl` / `stp_prmbnner` | 看图文本（`google/gemini-2.5-pro`） | — | — |
| `stp_t2imain` 文生图样张 | 图像（`openai/gpt-image-2`） | 2048×1360 | `stp_prmhero` |
| `stp_i2imain` 主图 | 图像（带参考图） | 1536×2048 | `stp_prmhero` |
| `stp_i2idetl` 详情图 | 图像（带参考图） | 1536×2048 | `stp_prmdetl` |
| `stp_i2ibnner` Banner | 图像（带参考图） | 2048×1152 | `stp_prmbnner` |

---

## 跑一遍

```bash
git clone <这个仓库> && cd ecom-details-image-local-run

# 1) 配置自己的 key（脚本会读 .env）
cp .env.example .env && $EDITOR .env

# 2) 冒烟：1 行 = 3 个文本 + 4 张图
python3 run_13rows.py --only-row 1

# 3) 13 行全量（行并发 13，图像同飞 6）
python3 run_13rows.py

# 4) 重出总览图
python3 tools/make_contact_sheet.py
```

**幂等**：已存在的产物会跳过，重复执行不会重复花钱；要强制重跑某一步，删掉对应文件再跑。

---

## 目录

```
├─ run_13rows.py                     主流程（7 步 + 并行 + 限流 + 重试）
├─ run-log.jsonl                     逐步骤原始记录（时间 / 耗时 / 模型 / 失败）
├─ run-summary.json                  汇总
├─ rows.jsonl                        13 行输入
├─ reference-product.jpg             参考图（米白头戴耳机）
├─ prompts/system-instructions.json  逐字提取的 3 条 systemInstruction（带 sha256）
├─ scripts/generate_image.py         上游脚本的本地增强版
├─ tools/extract_prompts.py          从 spec 提取提示词
├─ tools/make_contact_sheet.py       生成总览图
└─ out/row-01…13/                    每行：3 个 *.prompt.txt + 4 个 *.png
```

---

## 关键结论（详见 [`REPORT.md`](REPORT.md)）

- 13 行 52 张图 **全部成功**，总耗时 **6 分 8 秒**，图像模型全程 `openai/gpt-image-2`，未触发回落。
- 与平台 SkillBot 那 13 行相比：**主图 / Banner / 文生图样张基本一致**；唯一明显差异是**详情图本批更暗、对比更强**（亮度均值 139 vs 152，深色像素占比 0.071 vs 0.040）。
- 文本步骤保真：本地产出的主图 Prompt 与平台版结构、约束项、hex 写法同构。
- 成本：按脚本口径约 `$0.026/张`，52 张 ≈ **$1.35**。

## 踩过的坑（复现必读）

- **必须走代理**：直连该路由域名会被 Cloudflare 拦（`error code:1034`）。
- **文本请求要带浏览器 UA**：否则 Cloudflare 返回 `1010`（图像脚本与 runner 用的是同一个 UA）。
- **路由的 `/v1/models` 不列图像模型**：能不能用只能实跑验证；runner 内置回落（被拒时自动切 `openai/gpt-image-2.5-flare`）并在日志里标明实际模型。
- **路由不支持 `aspect_ratio`**：尺寸必须写像素；参考图字段名是 `images`（data URI 数组）。
- **白底不是严格纯白**（约 `250,251,252`）：要做 Amazon 主图需要后处理推到 `RGB(255,255,255)`。

---

## 来源与许可

- **上游项目**：[`liangdabiao/ecom-details-image`](https://github.com/liangdabiao/ecom-details-image)（模板与提示词体系来自该项目；上游按 `buluslan/gpt-image2-ecommerce` 的模板整理）。
- **`scripts/generate_image.py` 不是上游原文件**，是在上游版本上做的本地增强：`+183 / −26` 行，新增 **CogFoundry 路由模式（提交 → 轮询）**、`IMG_PROXY` 代理支持、网络抖动重试（上游 `a461fe04…` 18599 B → 本版 `70007b4f…d606` 25842 B）。上游仓库**未声明开源许可**，商用或再分发前请先与上游作者确认。
- **本仓库新增部分**：`run_13rows.py`、`tools/`、`REPORT.md`、总览图与 `out/` 下的产物。
- **不含任何 API key**：`.env` 已被 `.gitignore` 排除，仓库里只有 `.env.example`。
