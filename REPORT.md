# 本地复刻 SkillBot「ecom-details-image Pro 强构」· 13 行实测报告

**日期**：2026-09-18 17:55–18:02（北京时间）
**结论**：链路完全跑通，13 行 52 张图全部成功、0 失败，总耗时 **6 分 8 秒**；用的就是 SkillBot 那个图像模型 `openai/gpt-image-2`，未触发回落。产出与平台 SkillBot 高度一致，唯一明显差异是**详情图本地整体更暗、对比更强**。

---

## 一、这次跑的是什么

| 项 | 值 |
|---|---|
| 路由 | `https://router.cogfoundry.ai/api/v1`（走本地代理 `http://127.0.0.1:7890`） |
| 文本 / 看图模型 | `google/gemini-2.5-pro`（与那 13 行 SkillBot 实测一致） |
| 图像模型 | `openai/gpt-image-2`（**与 SkillBot 完全相同**，路由接受，无需回落） |
| 图像脚本 | 上游 `liangdabiao/ecom-details-image` 脚本的**本地增强版**：+183 / −26 行，新增 CogFoundry 路由模式、`IMG_PROXY` 代理支持、网络重试（上游 `a461fe04…` 18599 B → 本版 `70007b4f…d606` 25842 B） |
| 流程 | 与 SkillBot 的 7 步一一对应：3 个看图写 Prompt + 4 个出图，`dependsOn` 一致 |
| 提示词 | 从 `ecom-pro-v4.spec.json` **逐字提取**（`tools/extract_prompts.py`），非手抄 |
| 输入 | 13 行完全相同：brief「高端耳机」+ 同一张参考图（`reference-product.jpg`，1080×1440） |
| 并发 | 行并发 13，图像同飞上限 6 |

**出图尺寸**（照 SkillBot 实测比例定的，路由不支持 `aspect_ratio`，只能写像素）

| 步骤 | 尺寸 |
|---|---|
| `stp_t2imain` 文生图样张 | 2048×1360（3:2） |
| `stp_i2imain` 主图 | 1536×2048（3:4） |
| `stp_i2idetl` 详情图 | 1536×2048（3:4） |
| `stp_i2ibnner` Banner | 2048×1152（16:9） |

---

## 二、结果

| 项 | 值 |
|---|---|
| 行成功 | **13 / 13** |
| 图片产出 | **52 / 52** |
| 失败 | **0** |
| 模型回落 | 未触发（全程 `openai/gpt-image-2`） |
| 总耗时 | **368 秒（6 分 8 秒）**，其中 row 1 复用了冒烟结果 |
| 文本调用 | 36 次真实请求，平均 22.4s（16.2–32.5s） |
| 图像调用 | 48 次真实请求，平均 170s，最快 30.7s，最慢 335.4s（**含等信号量排队**，不是纯接口耗时） |
| 首次冒烟（1 行 4 图） | 64.4s，4/4 成功 |

---

## 三、与平台 SkillBot 的对比

对比对象：同一模板的 `rows-v3-13.json` 那一批（SkillBot 平台产出，13 行 ×4 图，本地已留档 `loomloom-ecom-run/artifacts-v3-13/`）。
并排图见 `contact-sheet-vs-skillbot.png`（每个步骤一块，上排 SkillBot、下排本地）。

### 客观指标（13 行均值）

| 步骤 | 指标 | SkillBot | 本地复刻 |
|---|---|---|---|
| 文生图样张 | 亮度均值 / 产品覆盖 | 239.3 / 0.220 | 240.1 / 0.204 |
| 主图（参考图） | 亮度均值 / 产品覆盖 | 237.6 / 0.263 | 237.4 / 0.261 |
| 详情图（参考图） | 亮度均值 | 151.7 | **139.4** |
| 详情图（参考图） | 深色像素占比（阈值 60） | 0.0399 | **0.0707** |
| Banner（参考图） | 亮度均值 | 223.5 | 217.2 |

> 「产品覆盖」= 明显偏离纯白的像素占比；「深色像素占比」沿用此前那份 bug 复盘的口径，阈值取 60（参考图自身 = 0.1876，仅作横向参照）。

### 视觉观察（看 `contact-sheet-vs-skillbot.png`）

1. **主图 / Banner / 样张**：两套基本看不出差别，都是白底 + 米白耳机，Banner 的产品在右三分之一、左边留白，构图和提示词要求一致。
2. **详情图**：这是唯一明显差异。本地 13 张整体更暗、对比更强，部分行出现深色背景和强侧光；SkillBot 那批更平、更亮。两批都符合「微距 + 强侧光」的提示词，但**本地这批的影调更重**。
3. **一致性**：SkillBot 那 13 行里，文生图样张出现过一次明显偏深色的耳机（第 8 行）；本地 13 行全部保持米白。**样本只有 13 行，不能当成稳定性结论。**

### 文本步骤保真度（第 1 行，主图 Prompt 并排）

| | SkillBot（v3） | 本地（v4 提示词） |
|---|---|---|
| 视角/主体 | 45 度、高端头戴耳机、哑光塑料+可调臂 | 同 |
| 颜色 | `#E3DED4`（耳罩同色）+ 金色点缀 `#B09B7A` | `#EAE6DD` + `#DED8CE`（耳垫）+ 古铜 `#B68D5B` |
| 背景/占比 | 纯白 `#FFFFFF`、约 60%、顶部与左上留空 | 同 |
| 否定清单 | 有 | 有 |
| 额外 | — | 多一句 v4 新增的构图锁（`Framing: … Do not change this framing.`） |

两条 Prompt 结构、约束项、颜色写法（hex）完全同构；色值有细微差异（模型读同一张图的正常波动）。本地之所以多出那句构图锁，是因为用的是**当前 v4 提示词**，SkillBot 那批留档来自 v3。

---

## 四、费用

- 本次**没有逐调用记录费用**（脚本会打印 `费用 $x`，但首轮运行未落盘；已补上，下次会写进 `run-log.jsonl` 与 `run-summary.json`）。
- 按 skill 自身口径 `≈$0.026/张`（2K）估算：52 张 ≈ **$1.35**。
- 对照：LoomLoom 那条 SkillBot 路径实测约 **1.386 元/张**，52 张 ≈ **72 元**。

---

## 五、产物

```
ecom-local-run/
├─ contact-sheet-local-only.png     只放本地这批：4 个步骤各一块 × 13 行（2852×1414，推荐看这张）
├─ contact-sheet-local.png          13 行 × 4 图总览（1370×5506）
├─ contact-sheet-vs-skillbot.png    SkillBot vs 本地 并排（2698×2376）
├─ run_13rows.py                    主流程
├─ run-log.jsonl                    逐步骤原始记录（含时间/耗时/模型/失败）
├─ run-summary.json                 最后一次调用的汇总（当前为缓存校验那次）
├─ rows.jsonl                       13 行输入
├─ reference-product.jpg            参考图
├─ prompts/system-instructions.json 逐字提取的 3 条 systemInstruction
├─ tools/extract_prompts.py         提取脚本
├─ tools/make_contact_sheet.py      总览图脚本
├─ scripts/generate_image.py        上游脚本的本地增强版（+CogFoundry 模式/代理/重试）
└─ out/row-01…13/                   每行 3 个 prompt.txt + 4 个 png
```

---

## 六、怎么复跑

```bash
cd ecom-local-run
python3 tools/extract_prompts.py            # 重新提取提示词（可选）
python3 run_13rows.py --only-row 1          # 冒烟 1 行
python3 run_13rows.py                       # 13 行全量
python3 run_13rows.py --image-concurrency 3 # 代理不稳时降并发
python3 tools/make_contact_sheet.py         # 重出总览图
```

已存在的产物会跳过，所以重复执行不会重复花钱。想强制重跑某一步，删掉对应文件再跑。

---

## 七、已知差异与限制

1. **图像模型是否可用只能实测**：路由的 `/v1/models` 只列 49 个文本模型，不列任何图像模型；本次是实跑验证 `openai/gpt-image-2` 可用。脚本里已内置回落：若被拒，自动切 `openai/gpt-image-2.5-flare`（本机验证过），并在报告里标明。
2. **尺寸是照比例反推的**：SkillBot 用的是 `size: auto`（模型自己定），路由不接受 `auto`，所以按平台实际产出比例写死像素。要看完全一致的行为，只能在平台上跑。
3. **详情图影调差异**：本地更深、对比更强，可能与尺寸/模型版本/参考图权重有关，本次未做归因实验。
4. **文生图样张天然弱一致**：`stp_t2imain` 不看参考图（与 SkillBot 一致），产品一致性依赖文本步骤写出的颜色描述。
5. **白底不是严格纯白**：主图背景约 `(250,251,252)`，Amazon 要求严格 `RGB(255,255,255)`。GitHub skill 自带的处理脚本在 `generated-images/cream-over-ear-headphones-pdp/fix-amazon-white.py`，本次**未执行**，保持原始模型输出以便对比。
6. **13 行是同一商品同一参考图**：这次测的是稳定性与吞吐，不是 13 个不同商品。
7. **代理是必须的**：直连该域名会被 Cloudflare 拦（`error code:1034`）；文本请求还会因 UA 被拦（`1010`），脚本与本 runner 都带了浏览器 UA。
