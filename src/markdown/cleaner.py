"""Markdown 清理模块(idea.md §7)。

原则:修复结构,不改写正文;不用 LLM 重写。
当前实现:
  - 统一换行符(CRLF → LF)
  - 围栏代码块 / 行内代码**掩码**:规则链全程不触碰代码,结束时逐字节还原
  - 页码残留剔除(独立纯数字行 1-3 位)
  - 页眉页脚重复行剔除(跨页反复出现的短行,扫描书收益最大)
  - 跨页断行连接(被页码/页脚/页码注释隔断或非标点结尾的连续段落)
  - OCR 异常空格合并(中文语境里被拆开的拉丁词,如 `Py Mu PDF`)
  - 中文排版空格修正(汉字-汉字、汉字-数字之间的空格)
  - 多余空行压缩(连续 ≥3 个空行 → 1 个)
  - 重复标题去重(相邻同名标题)/ 空标题删除 / 标题层级跳跃修正
  - 行尾空白清理
  - 诗行保护(连续短行不拼接 + 补 Markdown 硬换行)
  - 图片引用存在性校验
各项可用 CleanOptions 单独开关(配置 clean: 段 / CLI --clean-disable)。

新增启发式规则的共同原则:**默认保守 + 可单独关闭 + 配「不该改的例子」测试**。
不确定的改动一律不做(宁可留下噪声,也不破坏正文)。

两条结构性前提(改动清理器前先读):

1. **代码块不可改写**。所有规则都是「按行改写正文」的启发式,一旦打到围栏代码块或
   行内代码上就是语义损坏;因此规则链跑在掩码文本上,代码原文只在最后还原。
2. **页码注释(`<!-- page N -->`)不是内容**,它标记页边界。它既不该被拼接吃掉,
   也不该把「被页边界断开的段落」切断 —— 否则跨页连接这条规则在真实产物上完全失效。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from page_result import PAGE_MARK_RE

# 段落结尾标点:以此结尾的段落视为完整段落,不参与跨页拼接
END_PUNCT = set("。！？；：、，·…—”』」）】》%％\"'")
# 段落开头标点:以此开头的段落不与上一段拼接
START_PUNCT = set("“‘『「（【《〈\"'")
# markdown 块级标记:不参与拼接
BLOCK_MARKERS = ("#", ">", "|", "- ", "* ", "```", "<", "![", "+ ")
#: 有序列表标记(`1. ` / `12) `):markdown 里任意数字都成立。
#: 曾经只硬编码 `1. ` `2. `,于是目录/清单里第 3 条起的条目被当成正文,
#: 与相邻行拼成一整行(实测真实书籍的目录:`…/30` + 页码 `I` + `5. 失踪与疯癫/35`)。
ORDERED_ITEM_RE = re.compile(r"^\d{1,3}[.)](\s|$)")
#: 独立成行的页码注释(格式由 page_result 定义,这里只判断「整行就是一条注释」)
PAGE_MARK_LINE_RE = re.compile(rf"^\s*{PAGE_MARK_RE.pattern}\s*$")


def _is_block_start(s: str) -> bool:
    """行首是否为 markdown 块结构(标题/引用/表格/列表/代码/图片/HTML)。"""
    return s.startswith(BLOCK_MARKERS) or bool(ORDERED_ITEM_RE.match(s))


def _is_page_mark(line: str) -> bool:
    """整行就是一条页码注释(`<!-- page 12 -->`)。"""
    return bool(PAGE_MARK_LINE_RE.match(line.strip()))
# 中文字符集(汉字 + 常见中文标点),用于空格修正
CJK_CHARS = (
    "\u4e00-\u9fff"          # 汉字
    "\u3000-\u303f"          # 中文标点
    "\uff00-\uffef"          # 全角字符
    "\u201c\u201d\u2018\u2019\u2014\u2026"  # “”‘’—…
)

#: 跨空行拼接时,「前一行」最少字符数(去空白后)。
#: 空行在 Markdown 里通常是段落边界;只有足够长的一行才可能是被页码/页眉断开的
#: 前半句。短行(年份 `1984`、页眉、小标题)拼进下一段会把独立行粘成正文 ——
#: 这是比「漏拼一处」严重得多的误伤,所以这里取保守门槛。
CROSS_BLANK_MIN_CHARS = 10
#: 相邻行拼接时,「前一行」最少字符数(去空白后)。
#: PDF 里被断开的正文行通常是排满的整行(中文 20-40 字);而 5-7 字的短行更多是
#: 诗句/居中标题/短标签 —— 拼进去会毁掉诗的换行结构。门槛低到只挡这些短行,
#: 漏拼一处段落只是多一个换行,属于可接受的方向。
ADJACENT_MIN_CHARS = 6
#: 「诗行」上界:两行都不超过该长度、且都不以句末标点结尾 → **不拼接**,并在这之后
#: 补 Markdown 硬换行。两个动作必须用同一个门槛:既然已经判定「这不是被断开的段落」,
#: 就得同时在渲染层保住换行 —— 只判不拼的话,pandoc 仍会把段落内的软换行渲染成空格,
#: 13-18 字的现代诗/词照样在阅读器里挤成一行(硬换行门槛曾单独取 12,即此档漏网)。
#: 中文正文排满的整行通常 20 字以上(双栏样本实测 24-33 字),而律诗/绝句 5-7 字、
#: 词 3-9 字、现代诗多数 ≤18 字 —— 两者之间有很宽的间隔,阈值放在间隔里。
VERSE_MAX_CHARS = 18
#: Markdown 硬换行:行尾两个空格(pandoc/CommonMark 通用)。
HARD_BREAK = "  "
#: 标题行(`#` ~ `######` + 可选空格 + 文本)
HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$")
#: 行内 CJK 字符(判断「中文语境」)
CJK_RE = re.compile(rf"[{CJK_CHARS}]")

#: 围栏代码块标记(``` 开头即进入 / 退出代码块),与 markdown 惯例一致
FENCE_MARK = "```"
#: 行内代码:同一行内成对的单反引号片段(跨行、配不成对的一律不动)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
#: 占位符里用的哨兵字符:不可能出现在正常 Markdown 文本里
SENTINEL = "\x00"
#: 还原后仍残留的占位符(说明有规则改写了它 —— 那意味着代码块内容可能已损坏)
MASK_LEFTOVER_RE = re.compile(rf"{SENTINEL}[fc]\d+{SENTINEL}")


def _mask_code(md: str) -> tuple[str, list[tuple[str, str]]]:
    """把围栏代码块与行内代码替换成惰性占位符,返回 (掩码文本, [(占位符, 原文)])。

    为什么要整体掩码:清理规则全是「按行改写正文」的启发式,分头给每条规则加一次
    「跳过代码块」既容易漏,也会随新规则再次失守。实测误伤(未掩码时):
    围栏代码块里的 `123` / `32` 被当页码删除、`###` 被当空标题删除、
    `"这是 测试 文本"` 被中文空格修正改写、重复出现的 `end` 被当页眉剔除;
    行内代码 `` `git 中文 命令` `` 同样被改写。

    占位符的约束(缺一不可,否则它自己会被规则吃掉):
      - 唯一(不会触发「重复行」删除)、无 CJK、无空格、非纯数字;
      - 围栏占位符以 `<` 开头 —— 与 markdown 块结构同类,拼接规则既不会吃掉它,
        也不会把相邻正文拼进它。

    围栏内的原文**逐字节保留**(含行尾空白):未闭合的围栏一路保护到文件末尾。
    """
    lines = md.split("\n")
    out: list[str] = []
    blocks: list[tuple[str, str]] = []          # [(占位符, 原文)]

    def token(kind: str, text: str, wrapper: str = "") -> str:
        """登记一块原文并返回占位符(返回值即插入文本的字符串)。"""
        ph = f"{wrapper}{SENTINEL}{kind}{len(blocks)}{SENTINEL}{wrapper}"
        blocks.append((ph, text))
        return ph

    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith(FENCE_MARK):
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(FENCE_MARK):
            j += 1
        end = min(j + 1, len(lines))                  # 含收尾围栏;未闭合则到文件尾
        out.append(token("f", "\n".join(lines[i:end]), wrapper="<"))
        i = end

    masked = INLINE_CODE_RE.sub(lambda m: token("c", m.group(0)), "\n".join(out))
    return masked, blocks


def _unmask_code(md: str, blocks: list[tuple[str, str]]) -> str:
    """还原代码块原文。还原不完整说明有规则改写了占位符 —— 报错,不静默丢内容。

    只检查**占位符形状**而不是「文本里有没有哨兵字符」:输入文件本身带 NUL 时
    不该让整本书转换失败(那是上游的问题,不是清理器的)。
    """
    for placeholder, original in blocks:
        md = md.replace(placeholder, original)
    if MASK_LEFTOVER_RE.search(md):
        raise RuntimeError("清理器占位符还原失败(有规则改写了代码块占位符)")
    return md


#: 清理项开关名:config `clean:` 段、CLI `--clean-disable`、桌面端「清理选项」共用
CLEAN_KEYS = (
    "page_numbers",    # 剔除独立页码行
    "running_heads",   # 剔除页眉页脚重复行
    "join_lines",      # 跨页断行连接
    "ocr_spaces",      # OCR 异常空格合并
    "cjk_spaces",      # 中文排版空格修正
    "dup_headings",    # 相邻重复标题去重
    "headings",        # 空标题删除 + 层级修正
    "bold",            # 强调字体 → `**` 粗体(实际作用于 pymupdf 后端)
    "images",          # 图片引用存在性校验
)


@dataclass
class CleanOptions:
    """Markdown 清理项开关。

    默认值与 config.yaml 的 `clean:` 段、桌面端「清理选项」一致
    (bold 默认关:强调字体标注会让正文出现较多 `**`,需要时再开)。
    """

    page_numbers: bool = True   # 剔除独立页码行
    running_heads: bool = True  # 剔除页眉页脚重复行
    join_lines: bool = True     # 跨页断行连接
    ocr_spaces: bool = True     # OCR 异常空格合并
    cjk_spaces: bool = True     # 中文排版空格修正
    dup_headings: bool = True   # 相邻重复标题去重
    headings: bool = True       # 空标题删除 + 标题层级修正
    bold: bool = False          # 强调字体 → `**` 粗体(实际作用于 pymupdf 后端)
    images: bool = True         # 图片引用存在性校验

    @classmethod
    def from_config(cls, config: dict | None = None) -> "CleanOptions":
        cfg = (config or {}).get("clean") or {}
        opts = cls()
        for key in CLEAN_KEYS:
            if cfg.get(key) is not None:
                setattr(opts, key, bool(cfg[key]))
        return opts

    def disable(self, keys: Iterable[str] | str | None) -> None:
        for key in normalize_clean_keys(keys):
            setattr(self, key, False)

    def apply(self, config: dict) -> None:
        """把开关落到各模块实际读取的位置。

        bold 只在 pymupdf 后端生效(需要 PDF 的字体信息),关闭等价于
        `pymupdf.bold_fonts` 为空 —— 后端据此跳过字体标注。
        """
        if not self.bold:
            config.setdefault("pymupdf", {})["bold_fonts"] = []


def normalize_clean_keys(keys: Iterable[str] | str | None) -> list[str]:
    """规范化清理项名(接受 `-`/`_` 混用、逗号或空格分隔),未知项报错。"""
    if keys is None:
        return []
    if isinstance(keys, str):
        keys = re.split(r"[,\s]+", keys)
    out: list[str] = []
    for raw in keys:
        key = str(raw).strip().lower().replace("-", "_")
        if not key:
            continue
        if key not in CLEAN_KEYS:
            raise ValueError(f"未知清理项: {raw!r}(可选: {', '.join(CLEAN_KEYS)})")
        if key not in out:
            out.append(key)
    return out


def resolve_options(config: dict | None = None, disable: Iterable[str] | str | None = None) -> CleanOptions:
    """配置默认值 + 本次运行的关闭项 → 最终 CleanOptions。"""
    opts = CleanOptions.from_config(config)
    opts.disable(disable)
    return opts


@dataclass
class CleanReport:
    issues: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.issues.append(msg)


def _is_verse_line(line: str) -> bool:
    """诗行候选:不超过 ``VERSE_MAX_CHARS``、无句末标点、非 markdown 块结构。

    **引用块(`>`)里的诗是最常见的形态**,所以先把引用前缀剥掉再判定;
    其他块结构(标题/列表/表格/图片/代码)一律不碰。
    """
    s = line.strip()
    while s.startswith(">"):
        s = s[1:].strip()
    if not s or _is_block_start(s):
        return False
    if len(re.sub(r"\s+", "", s)) > VERSE_MAX_CHARS:
        return False
    return s[-1] not in END_PUNCT


def _mark_verse_lines(md: str, report: CleanReport) -> str:
    """给连续短行(诗行)加 Markdown 硬换行,免得诗在阅读器里被渲染成一行。

    判据与「不拼接」完全一致:连续 ≥ 2 行都不超过 ``VERSE_MAX_CHARS``、都不以句末标点
    结尾、都不是块结构(标题/引用/列表/表格/图片)。**空行会断开连续段**,所以空行分隔
    的单行诗不会被处理(分段留白本身是原样保留的)。页码注释行对连续性透明(见
    ``_is_page_mark``):一首诗跨页时,页边界不该在诗中间留下一处缺硬换行的行。

    只补不拼:本函数只加行尾两空格(``HARD_BREAK``),因此必须排在行尾空白清理之后;
    对同一份文本重复运行不会叠加空格(先 rstrip 再补),幂等。
    """
    lines = md.split("\n")
    out = lines[:]
    i = 0
    marked = 0
    while i < len(lines):
        if lines[i].strip().startswith("```"):        # 代码块整段跳过
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                i += 1
            i += 1
            continue
        if not _is_verse_line(lines[i]):
            i += 1
            continue
        run = [i]                                      # 连续诗行的行号
        cursor = i
        while True:
            k = cursor + 1
            while k < len(lines) and _is_page_mark(lines[k]):
                k += 1                                 # 页码注释透明
            if k < len(lines) and _is_verse_line(lines[k]):
                run.append(k)
                cursor = k
            else:
                break
        if len(run) >= 2:                              # 连续 ≥2 行才当诗
            for k in run[:-1]:                         # 段末行不需要硬换行
                out[k] = out[k].rstrip() + HARD_BREAK
                marked += 1
        i = cursor + 1
    if marked:
        report.add(f"诗行: 保留分行 {marked} 行")
    return "\n".join(out)


def clean_markdown(
    md_text: str,
    images_dir: Path | None = None,
    report: CleanReport | None = None,
    options: CleanOptions | None = None,
) -> str:
    """清理 Markdown 文本,返回清理后的内容。

    options=None → 各项按默认开关执行(等价 CleanOptions())。
    """
    report = report or CleanReport()
    options = options or CleanOptions()

    # 1. 统一换行
    md = md_text.replace("\r\n", "\n").replace("\r", "\n")

    # 1.5 代码掩码:围栏代码块与行内代码对下面所有规则免疫(规则链只跑在正文上)
    md, code_blocks = _mask_code(md)

    # 2. 剔除页码残留:独立纯数字行(1-3 位,前后可有空白)
    if options.page_numbers:
        md = re.sub(r"(?m)^[ \t]*\d{1,3}[ \t]*$\n?", "", md)

    # 3. 剔除页眉页脚重复行(必须在跨页拼接之前:否则页眉会被拼进正文)
    if options.running_heads:
        md = _strip_running_heads(md, report)

    # 4. 中文排版空格:中文(含中文标点)之间、中文与数字之间的空格
    #    (保留中英之间的空格)
    if options.cjk_spaces:
        md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=[{CJK_CHARS}])", "", md)
        md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=\d)", "", md)
        md = re.sub(rf"(?<=\d) (?=[{CJK_CHARS}])", "", md)
        # 内联 HTML 标签两侧(如 "<u>首" 前的空格)与半角括号两侧
        md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=<[^>]+>)", "", md)
        md = re.sub(r"(</?[a-zA-Z]{1,5}>) (?=[\u4e00-\u9fff])", r"\1", md)
        md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=[()])", "", md)
        md = re.sub(rf"(?<=[()]) (?=[{CJK_CHARS}])", "", md)

    # 5. 中间空行压缩(页码行删除后会留下连续空行,压缩到单个空行
    #    以便跨页断行拼接能跨越)
    md = re.sub(r"\n{3,}", "\n\n", md)

    # 6. 跨页断行连接(在页码删除与空格修正之后)
    if options.join_lines:
        md = _join_broken_lines(md)

    # 7. OCR 异常空格合并(在拼接之后:先还原段落,再修词内空格)
    if options.ocr_spaces:
        md = _fix_ocr_spaces(md, report)

    # 8. 重复标题去重 / 空标题与标题层级
    if options.dup_headings:
        md = _dedupe_headings(md, report)
    if options.headings:
        md = _normalize_headings(md, report)

    # 9. 压缩多余空行(3+ → 1,兜底)
    md = re.sub(r"\n{3,}", "\n\n", md)

    # 10. 行尾空白
    md = re.sub(r"[ \t]+$", "", md, flags=re.MULTILINE)

    # 10.5 诗行硬换行(必须在行尾空白清理之后:硬换行本身就是行尾两个空格)。
    #      不拼接只保住 book.md 的换行,pandoc 仍会把换行渲染成空格 —— 这一步
    #      才让诗在阅读器里真的分行。
    if options.join_lines:
        md = _mark_verse_lines(md, report)

    # 10.8 还原代码块(必须排在所有规则之后:代码块原文逐字节回写)
    md = _unmask_code(md, code_blocks)

    # 11. 图片引用存在性校验
    if options.images and images_dir is not None and images_dir.is_dir():
        existing = {p.name for p in images_dir.iterdir() if p.is_file()}
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", md):
            ref = m.group(1)
            name = Path(ref).name
            if name not in existing and not Path(ref).is_absolute():
                report.add(f"图片引用缺失: {ref}")

    return md


def _is_running_head_candidate(s: str, max_len: int) -> bool:
    """页眉页脚候选行:短、无句末标点、非 markdown 块结构。

    页眉页脚(书名/章节名/页码装饰)在正文里反复出现且**不带句末标点**;
    正文句子几乎总以标点结尾,列表/表格/标题/代码有块标记,都会被排除。
    """
    if not s or len(s) > max_len:
        return False
    if _is_block_start(s) or "[" in s or "]" in s:
        return False
    if s[-1] in END_PUNCT:
        return False
    if s.isdigit():
        return False                        # 纯页码交给 page_numbers
    return any(ch.isalnum() or CJK_RE.match(ch) for ch in s)


def _strip_running_heads(
    md: str,
    report: CleanReport,
    *,
    min_repeats: int = 3,
    max_len: int = 40,
) -> str:
    """删除页眉页脚重复行:同一短行在全文中重复出现 ≥ min_repeats 次。

    保守之处:只删「重复且短且无句末标点」的行;单次出现的短行一律保留。
    局限:同一页内重复出现的短句(如反复出现的口号)也会被删,可用
    `--clean-disable running_heads` 关闭。
    """
    lines = md.split("\n")
    counts = Counter(s for s in (line.strip() for line in lines)
                     if _is_running_head_candidate(s, max_len))
    dupes = {s for s, c in counts.items() if c >= min_repeats}
    if not dupes:
        return md

    kept: list[str] = []
    removed = 0
    for line in lines:
        if line.strip() in dupes:
            removed += 1
            continue
        kept.append(line)
    report.add(f"页眉页脚: 剔除重复行 {removed} 行({len(dupes)} 种)")
    return "\n".join(kept)


def _fix_ocr_spaces(md: str, report: CleanReport, *, max_frag: int = 4,
                    min_frags: int = 3, min_short: int = 2) -> str:
    """合并 OCR 把单个拉丁词拆开留下的空格(`Py Mu PDF` → `PyMuPDF`)。

    保守条件(缺一不可),避免误伤正常的英文短语(`the cat sat` 不满足②):
      ① 该行含中文 —— OCR 拆词几乎只发生在中文语境里;
      ② 连续 ≥3 个 ≤4 字母的拉丁片段,其中至少 2 个片段长 ≤2(拆开的碎片通常极短);
      ③ 片段之间只有单个空格,且两端不与其它字母相连。
    """
    frag = rf"[A-Za-z]{{1,{max_frag}}}"
    pattern = re.compile(rf"(?<![A-Za-z]){frag}(?: {frag}){{{min_frags - 1},}}(?![A-Za-z])")
    hits = 0

    def fix_line(line: str) -> str:
        nonlocal hits
        if not CJK_RE.search(line):
            return line

        def repl(m: re.Match[str]) -> str:
            nonlocal hits
            parts = m.group(0).split(" ")
            if sum(1 for p in parts if len(p) <= 2) < min_short:
                return m.group(0)
            hits += 1
            return "".join(parts)

        return pattern.sub(repl, line)

    fixed = "\n".join(fix_line(line) for line in md.split("\n"))
    if hits:
        report.add(f"OCR 空格: 合并拆开的拉丁词 {hits} 处")
    return fixed


def _dedupe_headings(md: str, report: CleanReport, *, max_blanks: int = 1) -> str:
    """相邻重复标题只保留一条(提取/OCR 常在页眉处重复输出同一标题)。

    只在「中间只有 ≤1 个空行、级别与文字都相同」时判定为相邻重复;
    被正文隔开的同名标题(如两章各有一个「小结」)一律保留,
    级别不同的同名标题(书名标题 + 章标题)也保留。
    """
    lines = md.split("\n")
    out: list[str] = []
    removed = 0
    prev_heading: tuple[int, str] | None = None
    blanks = 0

    for line in lines:
        s = line.strip()
        if s == "":
            blanks += 1
            out.append(line)
            continue
        m = HEADING_RE.match(s)
        if m is None:
            prev_heading = None
            blanks = 0
            out.append(line)
            continue
        key = (len(m.group(1)), m.group(2).strip())
        if key[1] and prev_heading == key and blanks <= max_blanks:
            removed += 1
            prev_heading = None   # 连续三个同名标题也只留一个
            continue
        prev_heading = key if key[1] else None
        blanks = 0
        out.append(line)

    if removed:
        report.add(f"重复标题: 去重 {removed} 行")
    return "\n".join(out)


def _normalize_headings(md: str, report: CleanReport) -> str:
    """空标题删除 + 标题层级修正。

    修正规则(只动 `#` 的个数,不改标题文字):
      - 首个标题若层级 >1 → 提升为 1 级(文档理应有 1 级标题)
      - 层级跳跃(如 `#` 后直接 `###`)→ 收为「上一级 +1」
    """
    lines = md.split("\n")
    out: list[str] = []
    dropped = 0
    promoted = 0
    leveled = 0
    prev_level: int | None = None

    for line in lines:
        m = HEADING_RE.match(line.strip())
        if m is None:
            out.append(line)
            continue
        level = len(m.group(1))
        text = m.group(2).strip()
        if not text:
            dropped += 1
            continue
        if prev_level is None:
            if level > 1:
                level = 1
                promoted += 1
        elif level > prev_level + 1:
            level = prev_level + 1
            leveled += 1
        prev_level = level
        out.append("#" * level + " " + text)

    if dropped:
        report.add(f"空标题: 删除 {dropped} 行")
    if promoted or leveled:
        report.add(f"标题层级: 修正 {promoted + leveled} 处")
    return "\n".join(out)


def _join_broken_lines(md: str) -> str:
    """跨页断行连接(结构感知)。

    代码块(``` 包裹)与表格(连续 | 行)作为整体单元保留内部换行;
    普通文本行之间,若前段不以结束标点结尾、后段不以开始标点/
    markdown 块标记开头(允许中间隔 1 个空行,如被删除页码留下的),
    视为同一段落被断行,拼接。

    **页码注释(`<!-- page 12 -->`)是透明单元**:它标记页边界,不是内容,
    既不参与拼接也不打断拼接。产物里每页之间都有一条注释(PyMuPDF / OCR 两条
    链路都会写),不透明的话「被页边界断开的段落」永远拼不上 —— 该规则会在
    真实产物上整体失效。拼接发生时注释落在拼接后的段落上方,注释本身一条不少。

    不拼接的单元之间**还原原有空行数**:旧实现统一按一个空行重建,会把
    紧凑列表与引用块的连续行拆成松散段落(渲染出多余段距),属于无谓的结构改写。
    """
    lines = md.split("\n")
    units: list[tuple[str, str]] = []  # (kind, text),kind: code/table/text/mark

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if _is_page_mark(s):
            units.append(("mark", s))
            i += 1
        elif s.startswith("```"):
            j = i
            buf = [lines[i]]
            j += 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            if j < len(lines):
                buf.append(lines[j])
            units.append(("code", "\n".join(buf)))
            i = j + 1
        elif s.startswith("|"):
            j = i
            buf = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                buf.append(lines[j])
                j += 1
            units.append(("table", "\n".join(buf)))
            i = j
        else:
            units.append(("text", s))
            i += 1

    out: list[tuple[str, str, int]] = []      # (kind, text, 之前的空行数)
    pending: tuple[str, str, int] | None = None
    marks: list[tuple[str, int]] = []         # 悬空的页码注释(文本, 之前的空行数)
    blanks = 0

    for kind, text in units:
        if kind == "text" and text == "":
            blanks += 1
            continue
        if kind == "mark":
            marks.append((text, blanks))       # 透明:先挂起,归属由下一个单元决定
            blanks = 0
            continue
        if pending is None:
            out.extend(("mark", t, g) for t, g in marks)   # 文档开头的注释
            marks = []
            pending = (kind, text, blanks)
            blanks = 0
            continue
        pk, pt, pb = pending
        gap = blanks
        prev_len = len(re.sub(r"\s+", "", pt))
        cur_len = len(re.sub(r"\s+", "", text))
        # 诗行对:两行都短、且**两行都没有句末标点** —— 换行是有意的,不能拼。
        # 只看长度挡不住「短行被拼」(7 字律诗必踩);只不看标点又挡不住正常的
        # 短行散文断行(样本 join_lines 的 9 字 + 14 字那对),所以两个条件都要。
        verse_pair = (
            prev_len <= VERSE_MAX_CHARS
            and cur_len <= VERSE_MAX_CHARS
            and text[-1] not in END_PUNCT
        )
        joinable = (
            pk == "text"
            and kind == "text"
            and gap <= 1
            and not _is_block_start(pt)
            and not _is_block_start(text)
            and pt[-1] not in END_PUNCT
            and text[0] not in START_PUNCT
            # 短行不拼(诗句/年份/页眉/小标题):跨空行比相邻更保守
            and prev_len >= (CROSS_BLANK_MIN_CHARS if gap >= 1 else ADJACENT_MIN_CHARS)
            and not verse_pair
        )
        if joinable:
            out.extend(("mark", t, g) for t, g in marks)   # 注释先落地
            marks = []
            # 中英文断行拼接:两侧均为拉丁字母时补空格,否则直接相连
            sep = ""
            if pt and text and pt[-1].isascii() and pt[-1].isalpha() \
                    and text[0].isascii() and text[0].isalpha():
                sep = " "
            pending = ("text", pt + sep + text, pb)
        else:
            out.append(pending)
            out.extend(("mark", t, g) for t, g in marks)
            marks = []
            pending = (kind, text, gap)
        blanks = 0
    if pending is not None:
        out.append(pending)
    out.extend(("mark", t, g) for t, g in marks)

    pieces: list[str] = []
    for kind, text, gap in out:
        if pieces:
            pieces.append("\n" * (gap + 1))
        pieces.append(text)
    return "".join(pieces)


def clean_file(book_md: str | Path, options: CleanOptions | None = None) -> CleanReport:
    """就地清理 book.md 文件。"""
    book_md = Path(book_md)
    text = book_md.read_text(encoding="utf-8", errors="replace")
    report = CleanReport()
    cleaned = clean_markdown(
        text,
        images_dir=book_md.parent / "images",
        report=report,
        options=options,
    )
    if cleaned != text:
        book_md.write_text(cleaned, encoding="utf-8")
    return report
