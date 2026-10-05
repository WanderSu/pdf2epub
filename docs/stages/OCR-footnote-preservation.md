# 阶段档案：OCR 脚注保留与原生 EPUB 脚注链路

> OCR Footnote Preservation & Native EPUB Footnote Pipeline
>
> 阶段结论基线：`375 passed, 2 skipped`；
> 起始提交 `a2cdf13`，收尾提交 `dd2bb4d`。
>
> 本文是**维护者视角的阶段档案**，不是代码文档，也不重复 `IDEA.md` 的设计说明：
> 它记录「这一段做过什么、为什么这样做、现在的边界在哪、下一步从哪接」。
> 实现细节以源码与 `IDEA.md` 为准，本文与实现不一致时以实现为准。

---

## 1. 阶段目标

让 OCR 真正识别到的脚注**贯穿全链路不丢**，并且最终变成 EPUB 阅读器里的**原生脚注**：

```text
正文……[^1]        ← 可点击
[^1]: 注释内容     ← 可返回正文
```

而不是把注释当普通文本堆在页脚，或干脆在 OCR 结果里消失。

**不在本阶段范围内**：PDF 原生 Annotation（高亮/便签）、完整 Document Model、
把 JSON 直接接到 EPUB、多编号体系（阿拉伯数字/`[1]`/`*`）、1 条注释对 N 个引用。

---

## 2. 问题背景：脚注到底在哪一层丢的

最初的症状是「EPUB 里脚注在错的位置、点不动、也回不去」。真实排查后确认**不是一处**，
而是**三个独立的丢失点**：

| 层 | 真实原因 |
|---|---|
| OCR（PaddleOCR-VL） | 云端 `markdown_ignore_labels` 默认值**含 `footnote`**，不显式传这个字段时脚注整批不出现在返回的 Markdown 里 —— 是配置丢的，不是解析丢的 |
| OCR（MinerU） | `page_footnote` 属于 discarded blocks，`full.md` **一个字都没有**（markdown 渲染只走正文块），只存在于 `content_list.json` |
| Markdown Cleaner | `[^1]: …` 定义行在规则眼里就是普通文本，「合并断行」会把它拼进正文段 → pandoc 再也认不出脚注，整批注释降级成正文文字 |
| 产物侧 | 即使 Markdown 里有脚注，pandoc 的 epub3 writer 也只写正文侧 `noteref`，**不写**脚注侧回链 |

定位方法（可复用）：用真实扫描书**逐层取证**（结构化结果 → 分片缓存 → 合并后的
`book.md` → Cleaner 之后 → EPUB 内部 XHTML），而不是读代码猜。脚注数在某一层
断崖（如 `content_list.json` 10 条 → `full.md` 0 条）就是丢失点。

---

## 3. 最终架构

```text
                    ┌─ MinerU: content_list.json ─┐
PDF → OCR ──────────┤                             ├→ Footnote Engine → Markdown
                    └─ PaddleOCR: parsing_res_list┘        (src/markdown/footnotes.py)
                                                                  ↓
                                                          Markdown Cleaner
                                                                  ↓
                                                        Pandoc → EPUB3
                                                                  ↓
                                            epub:type="footnote" / noteref / doc-backlink
```

- **MinerU**：`full.md` 不可作为脚注的唯一事实来源，必须从 `content_list.json` 取
  `page_footnote`。
- **PaddleOCR-VL**：结果 JSONL 每页带 `prunedResult.parsing_res_list`，其中
  `block_label == "footnote"` 就是页底脚注（另有 `block_bbox` / `block_content` /
  `block_order` 与 `dataInfo.pages[]` 的页面尺寸）。
- 两个后端都把自己的结构化结果**归一化成同形块**后交给同一个引擎。之所以不是
  「PaddleOCR JSON → EPUB」：Markdown 必须是可见、可检查、可编辑、可调试的中间结果，
  而 EPUB 生成只认 Pandoc；JSON 直连 EPUB 等于把两个后端的语义差异硬塞进产物层，
  等于放弃 Cleaner 与内容校验，也等于为每个后端再写一套渲染。

---

## 4. 关键设计决策（架构规则，后续开发必须遵守）

### 4.1 OCR 后端只提供结构化事实，脚注语义由引擎解释

```text
MinerU Adapter ──────┐
                     ├→ Footnote Engine（唯一实现）
PaddleOCR Adapter ───┘
```

- **不要**为新的 OCR 后端复制 `src/markdown/footnotes.py`，也**不要**在引擎里写
  `if backend == "..."`。新后端只需实现「结构化结果 → 统一块」的 Adapter。
- 统一块形状（两个后端一致，落在 `_parts/<段>/content_list.json`）：

```text
{type: "text" | "page_footnote", text, page_idx, bbox(0-1000 归一化), label?}
{type: "page_break", page_idx, offset}          # 可选：精确页边界
```

### 4.2 引擎只做保守、可验证的配对

> **错误链接比不链接更危险。**

配对规则（`match_footnotes`）：按 marker 分组 → 两边**数量相等** → 组内按阅读顺序
**一一对应** → 每对过**页窗**（脚注页 = 引用页，或紧随其后一页）；任何一对不过页窗，
**整组都不转**。数量不等时只接受「同页唯一」/「相邻页唯一」候选；有歧义或证据不足
一律不转。同页 = `high`、相邻页 = `medium`，只有这两档会变成脚注。

### 4.3 页归属用「精确页边界」而不是文本相似度

PaddleOCR 的 Markdown 由每页文本拼接而成，**拼接方知道每页的起点**，所以由适配器写
`page_break{page_idx, offset}`，引擎优先用它。文本对齐（去空白取 24 字前缀 + 页号非递减）
只作为没有页边界时（如 MinerU）的兜底。原因见 §5.4。

### 4.4 文本 offset 一旦产生就必须在同一次编辑里用掉

先删/改文本、再拿旧 offset 去替换，偏移就全废了（实测表现为同 marker 跨页的引用连到错页、
高置信度被降级）。规则：**所有改动（marker 替换、清掉已有的脚注原文行、插入脚注定义）
在同一个坐标空间里一次逆序应用**，绝不先把文本改掉再对齐。

### 4.5 Markdown 仍是主链路，Cleaner 不得破坏脚注语义

结构化结果只做 `JSON → Markdown`；Cleaner 可以清理正文，但必须保证已有的
`[^1]` / `[^1]: …` 结构原样通过（实现：整块掩码，见 §5.1）。

---

## 5. 关键工程问题与解决（真实踩过的坑）

### 5.1 Cleaner 破坏脚注

- 现象：`[^1]: …` 被「合并断行」拼进正文段 → pandoc 认不出脚注；内容对照校验也看不见
  （源里的定义已被吃掉）。
- 解决：清理前**整块掩码**脚注定义块（定义行 + 缩进 ≥4 空格的续行，与 pandoc 语法一致），
  规则链跑在掩码文本上，末尾逆序还原。掩码是唯一机制，避免每条规则各加一次「跳过」。
- 同时收紧了「合并断行」的启发式（拼接门槛改为「上一行是否排满」而不是「上一行有没有句末标点」），
  这部分属相邻的 Cleaner 专项，见提交 `81b1309`。

### 5.2 MinerU `full.md` 不含脚注

`page_footnote` 是 discarded blocks。因此 `*_content_list.json` 必须随段缓存落盘，
合并阶段再从它恢复 —— **不能把 OCR 后端的 Markdown 当成唯一事实来源**。

### 5.3 结构化结果必须进缓存，且写入顺序不能错

`_parts/<段标识>/` 里除段 Markdown 与图片外，还带 `content_list.json`。
`PartStore.save(..., extra_files=...)` 的写入顺序是：内容 → 附带文件 → **最后**写
`.part.json`（元数据只是「这段可用」的标记）。顺序颠倒会造出「缓存命中但脚注数据缺失」
的假缓存。老缓存没有附带文件时：照常复用、不报错、不重新 OCR（云端已计费，
不为一个附带文件让用户重跑一遍），只是那段重建不出脚注。

### 5.4 PaddleOCR 的页边界不能靠文本猜

重复页眉/刊名（如每页顶部都是 `社会科学战线·2007年第4期…`）会让「块 ↔ Markdown」
文本对齐命中外侧那一份，游标一次性被拖远，**后面整页的块全部对不上** —— 实测一本书的
全部引用被算到第 1 页，脚注 0/6 一条也配不上。修法不是调对齐阈值，而是让拼接方写
`page_break{page_idx, offset}`（先按页归一化图片引用**再**拼接，偏移才准）。
实测：`0/6 → 4/6`、`6/25 → 12/25`。

### 5.5 行首连续圈码

`⑥⑦同上書第64頁。` 这种「一条注释一行、行首一串 marker」的形态：如果只排除行首第一个
标记，`⑦` 会被当成正文引用，计数凭空多一个 → 整组配对偏（实测同一本书因此少配 3 条）。
规则：**行首是圈码的行，整行都不算引用**。

### 5.6 产物侧假失败：`$ ^{⑧} $` 不是公式

OCR 会把脚注标记包成行内公式（`$ ^{⑧} $`，开 `$` 后有空格）。pandoc 的定界规则是
「开 `$` 后不能紧跟空白、收 `$` 前不能是空白、收 `$` 后不能紧跟数字」，所以这类写法
pandoc 只按字面量输出，产物里当然没有 MathML。内容校验若按宽松 `$...$` 计数，会把整本书
误判「公式全部丢失」。计数必须按 pandoc 规则来。

---

## 6. 数据流（一次转换里脚注都经过什么）

```text
1. OCR 提交（两个后端都按内容指纹 + OCR 参数指纹缓存，不重复计费）
2. 结果落盘：_parts/<段>/full.md + images/ + content_list.json（归一化块）+ .part.json
3. 合并/复用：读 content_list.json → 引擎
4. 引擎：结构化脚注 + 引用 marker → 配对（数量/顺序/页窗）→
        生成 `正文[^1]` 与 `[^1]: 注释`；配对不上则原样保留普通文本
5. Cleaner：脚注定义块掩码保护，其余规则照常
6. Pandoc：Markdown → EPUB3（`epub:type="footnote"` + `noteref`）
7. 产物侧：补 `doc-backlink` 回链（pandoc 不写；只在产物确实含脚注时才重写 zip，
   `mimetype` 必须仍是第一个条目且不压缩）
8. 校验：内容对照（md 脚注定义数 ↔ EPUB 脚注数）+ 容器校验
```

---

## 7. 缓存与续跑策略

| 层 | 位置 | 作用 |
|---|---|---|
| 任务缓存 | `work/<书>/.ocr_task.json` | 已提交未取回的任务（`batch_id`/`job_id`），重跑继续轮询，**不重新上传** |
| 段结果缓存 | `work/<书>/_parts/<段标识>/` | 已拿到的结果（`full.md` + `images/` + `content_list.json` + `.part.json`），云端成功即已计费，之后一律复用 |

- 缓存键 = 后端 + 源文件指纹 + **OCR 参数指纹** + 段标识。只把**真正影响 OCR 输出**的
  配置放进指纹（如 `markdown_ignore_labels`）；纯本地后处理参数不进指纹。
- 脚注重建发生在**复用/合并时**，缓存里存的是未加脚注的原始 Markdown —— 规则改进了，
  老缓存不用重新 OCR 也能得到新结果。

---

## 8. 安全策略（脚注引擎的行为契约）

```text
high  (同页 + marker 一致 + 数量/顺序成立)  → 自动转成 Pandoc 脚注
medium(相邻页 + 同上)                      → 转成 Pandoc 脚注
low / 证据不足 / 有歧义                    → 不转：正文 marker 不动、脚注原文保留
```

- 禁止为了「匹配率好看」强行配对。
- 未配对的脚注**内容不丢**：以原文（含 marker）留在它所在页的末尾，且不会出现两份
  （已经带在 Markdown 里的那份会在同一次编辑里被清掉）。
- 有未匹配脚注时发 `footnotes_unmatched` warning 事件，不静默。

---

## 9. 当前支持范围

**支持**：

```text
① … ⑳                      正文引用与注释 markerr
$ ^{①} $                    PaddleOCR 实际输出形态（行内公式包裹）
⑥⑦同上書第64頁。             行首连续 marker 的注释行（整行不算引用）
```

**暂不自动处理**（属支持范围限制，不是 bug）：`*`、`·`（OCR 把 ① 认错）、`(7)`、`[7]`、
阿拉伯数字、1 条注释 ↔ N 个引用（`⑥⑦同上書…`、`①② 高鸿业…`）。

---

## 10. 已知限制

1. **一条注释对应多个 marker**：目前只认第一个 marker，另一侧的引用会成为孤立引用。
   这是下一阶段最值得解决的问题（真实书里孤立引用 18 / 10 个都与此有关）。
2. **OCR 把 marker 认错**（`①` → `·`、`$ ^{*} $`）：不猜测，保留原文。
3. **PaddleOCR 会把期刊页脚标成 `footnote`**（`CC BY-NC-SA 3.0`、
   `The World Association for Political Economy`）：会作为「无 marker 脚注」留在正文末尾，
   不删、不强行链接。
4. **MEDIUM 档的残余风险**：若 OCR 同时漏掉一个引用**和**一条同 marker 注释，数量仍相等、
   顺序会整体错位。「严格同页」的 HIGH 没有这个问题。
5. **OCR 的 `$ x $`（`$` 后有空格）不会被 pandoc 渲染成公式**：属 OCR 数学归一化问题，
   与脚注引擎无关，单独处理。
6. **未匹配引用保留 OCR 原样**（`$ ^{①} $` 字面量会显示在阅读器里）；是否把「确认不了但
   肯定是标记」的形态归一化成 `①` 尚未决定。

---

## 11. 真实验证（真实书，非 mock）

| 书 | 后端 | 页 | 结构化脚注 | 成功匹配 | 内容完整性 |
|---|---|---:|---:|---:|---:|
| 公社与公社所有制诸形态 | PaddleOCR | 8 | 44 | 29（high 26 / medium 3） | 44/44 |
| 自然辩证法，还是社会历史辩证法 | PaddleOCR | 7 | 6 | 4（high 3 / medium 1） | 6/6 |
| 以马克思主义为指导……访谈 | PaddleOCR | 13 | 25 | 12（high 12） | 25/25 |
| 巴勒斯坦问题的由来和发展 | MinerU | 72 | 10 | 10（high 4 / medium 6） | 10/10 |

产物侧逐本核对：`epub:type="footnote"` 数 = `noteref` 数 = `fnref` 数 = `doc-backlink` 数，
孤立引用 0、孤立脚注 0，XHTML 全部良构，`mimetype` 首条且不压缩，
转换日志 `[verify] 0 失败 0 警告`。

---

## 12. 测试基线

```text
375 passed, 2 skipped
```

（2 skipped = 需要真实云端 Token 的 live 用例，用 `PDF2EPUB_LIVE_OCR=1` 显式开启。）

覆盖范围：脚注引擎配对与 fallback、MinerU 适配、PaddleOCR 适配（含归一化、页边界、
公式不误伤、resume/老缓存）、Cleaner 脚注保护、内容校验计数、EPUB 原生脚注 + 回链、
Cleaner → Pandoc → EPUB 全链路。

**回归判据**：以后出现 `375 → 374`，先检查是否回归了本阶段（尤其脚注相关用例）。

---

## 13. Git 历史

| 提交 | 解决什么 |
|---|---|
| `a2cdf13` fix: preserve OCR footnotes through Markdown and EPUB pipeline | 脚注「内容不丢」：PaddleOCR 显式传 `markdownIgnoreLabels`（官方默认值 − `footnote`）并进指纹；MinerU 从 `content_list.json` 补回 `page_footnote`；Cleaner 掩码保护 `[^1]: …`；附带文件随段缓存落盘 |
| `e340191` feat: reconstruct native EPUB footnotes from OCR structure | 脚注「语义关系」：新增 `src/markdown/footnotes.py`（配对 + 置信度 + 稳定编号 + fallback），MinerU 接上；产物侧补 `doc-backlink`；结构化 → Cleaner → Pandoc → EPUB 全链路用例 |
| `dd2bb4d` feat: unify PaddleOCR structured footnotes into the shared engine | 后端统一：PaddleOCR `parsing_res_list` → 统一块 Adapter；页边界 `page_break` 取代文本对齐猜页；行首连续 marker 不算引用；行内公式计数按 pandoc 规则 |

相邻提交（同期、属 Cleaner 专项而非脚注链路）：`81b1309` fix: harden markdown cleaning
and preserve MinerU footnotes —— 收紧「合并断行」的拼接门槛、结构行不重写。

---

## 14. 下一阶段建议（仅建议，未实现）

- **P1 一条注释对应多个 marker**：为 `⑥⑦同上書第64頁。` / `①② 高鸿业…` 建立
  `1 footnote ↔ N references` 语义。这是当前孤立引用的主要来源。
- **P2 扩展 marker 识别**：`[1]` / `(1)` / 阿拉伯数字 / `*` —— 必须有严格的上下文判据
  （正文数字与注释编号无法从形态上区分，宁可继续不配对）。
- **P3 PaddleOCR 页脚误标为 footnote**：评估是否按「无 marker + 位置固定在页脚」过滤，
  或只在未配对时丢弃（注意：不能因此丢内容）。
- **P4 OCR 数学归一化**：`$ \xxx $`（`$` 后有空格）导致 pandoc 不渲染，属独立专项。

---

## 15. 阶段结论（给未来的自己）

这个阶段解决了「扫描书里的脚注到最后只剩一堆文本」的问题。以前的问题是：脚注在三个地方
各自丢一次 —— PaddleOCR 云端默认把 `footnote` 过滤掉了；MinerU 的 `full.md` 根本不输出
页脚脚注；即使脚注活到 Markdown，清理器的「合并断行」也会把 `[^1]: …` 拼进正文，pandoc
于是认不出脚注；最后即便认得出，产物里也只有可点的引用、没有回到正文的回链。

现在：两个云后端的结构化结果都归一化成同一形状，交给**同一个脚注引擎**配对（数量相等 +
阅读顺序 + 页窗三重证据），配对成功的写成 `正文[^1]` 与 `[^1]: 注释`，由 pandoc 生成
EPUB 原生脚注，产物侧再补齐 `doc-backlink`。配对不成立的一律**保持普通文本**，
内容一条不丢 —— 宁可少链接，也不做错误链接。设计上坚持「后端只提供结构化事实、
引擎解释脚注语义」，以后接新 OCR 只需要写 Adapter，不再复制一套匹配算法。

真实书验证：四本（PaddleOCR 三本 + MinerU 一本，共 85 条结构化脚注）里 55 条成功匹配，
其余按原文保留；产物侧 `footnote` / `noteref` / `doc-backlink` 数量一致、孤立项为 0。

还剩：**一条注释对多个 marker**（当前孤立引用的主因，最值得先做）、非圈码编号、
OCR 认错 marker、PaddleOCR 误标页脚、OCR 的 `$ x $` 数学不渲染。测试基线 375 passed。
