"""Markdown 文本判据(中文排版的「什么算什么」词汇表)。

这个模块只回答**判定问题**,不做任何改写:

    _is_block_start("## 标题")        → True   这是不是一个块结构行
    _is_catalog_entry("第一章 …/35")  → True   这是不是目录条目
    _keep_line_break("床前明月光")     → True   这一行要不要靠硬换行保住分行
    _looks_like_layout_line("书号 …") → True   这是不是版权页那种字段行

**为什么单独一个模块**:这些判据以前长在 ``markdown/cleaner.py`` 里,和"按行改写
整篇文本"的规则混在一起。P6 把清理执行搬到 IR/Patch 体系后,判据仍然是共享的
(``clean.rules`` 逐条 import),但改写不再由这里负责 —— 判据与执行分开,才可能
让"规则改了哪一块、凭什么"逐条审计。

阈值也在这里(``JOIN_MIN_CHARS`` / ``VERSE_MAX_CHARS`` / ``LAYOUT_MAX_CHARS``),
每条都带着当时的实测理由:这些数字是**取舍**,不是常量。改它们等于改清理行为,
必须同时改 golden 样本(tests/fixtures/cleaner/)并说明原因。
"""
from __future__ import annotations

import re

from page_result import PAGE_MARK_RE

# ---------------------------------------------------------------- 标点与块结构

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


# ---------------------------------------------------------------- 字符类

# 中文字符集(汉字 + 常见中文标点),用于空格修正
CJK_CHARS = (
    "\u4e00-\u9fff"          # 汉字
    "\u3000-\u303f"          # 中文标点
    "\uff00-\uffef"          # 全角字符
    "\u201c\u201d\u2018\u2019\u2014\u2026"  # “”‘’—…
)
#: 行内 CJK 字符(判断「中文语境」)
CJK_RE = re.compile(rf"[{CJK_CHARS}]")

#: 行内代码:同一行内成对的单反引号片段(跨行、配不成对的一律不动)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

#: 围栏代码块标记(``` 开头即进入 / 退出代码块),与 markdown 惯例一致
FENCE_MARK = "```"


# ---------------------------------------------------------------- 阈值

#: 参与拼接的「上一行」最少可见字符数(**拼满一行的门槛**)。
#: 判据换过一次:旧值是相邻 6 字 / 跨空行 10 字,只挡得住一眼可见的短行(诗、年份),
#: 挡不住图片说明、小节标题、版权页字段 —— 它们在真实扫描书里正好是 11-19 字:
#:
#:   真实 72 页扫描书(MinerU)实测 13 处拼接里 **12 处是误伤**:
#:     prev=11 `中东"不战不和"的僵局` + 正文段   ← 小节标题被吞进正文
#:     prev=14 `威武雄壮的巴勒斯坦游击队战士` + 正文段 ← 图片说明被吞进正文
#:     prev=12 `中国青年出版社印刷厂印刷` + `787×1092毫米32开本…` ← 版权页字段被粘成一行
#:   唯一正确的拼接 prev=41(`一九七三年十月战争后,埃及士兵站在已被摧毁的"巴" + `线",是…`)
#:
#: 结论:「没有句末标点」只是**必要**条件,真正能说明「这是被断行的一行」的是**这一行
#: 排满了**。中文正文排满的一行实测 20 字以上(双栏样本 24-33 字),段落末行则以标点
#: 收尾(已被 END_PUNCT 排除),所以门槛放在 20。宁可少拼一处(段落被页边界切成分段的
#: 视觉效果),也不要把标题/说明/字段吞进正文。
JOIN_MIN_CHARS = 20
#: 跨空行拼接的门槛。与相邻拼接取同一值:断行跨不跨空行,和「这一行是否排满」无关,
#: 旧值(6 / 10)的差异没有证据支撑。
ADJACENT_MIN_CHARS = JOIN_MIN_CHARS
CROSS_BLANK_MIN_CHARS = JOIN_MIN_CHARS
#: 版式字段行的长度上界 = 拼接门槛(``JOIN_MIN_CHARS``):短于「排满一行」的行,
#: 又没有句末标点,就算字段行(见 ``_looks_like_layout_line``)。
LAYOUT_MAX_CHARS = JOIN_MIN_CHARS

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

#: 目录 / 索引条目行的行尾:页码(可带 `/`、点线、省略号、`·` 或空格引导)。
#: 真实书籍的目录就是这种形态(`第一章 失踪与疯癫/35`、`第五章……204`),而它**没有
#: 句末标点**、长度也常常超过诗行门槛 —— 修复前这类行会被拼接成一整行
#: (实测三条目录章节被粘成 `第一章…/1第二章…/15第三章…/32`)。
ENTRY_TAIL_RE = re.compile(r"(?:/|\.{2,}|…+|·{2,}|\s|^)\s*\d{1,4}\s*$")

#: 注释/编号行:以 ①-⑳ 这类圈码序号开头。
#: 页脚脚注在产物里就是这个形态(`① 列宁:《告犹太工人书》…`),MinerU 补回的页脚脚注
#: 也正是这种独立段落 —— 它们和正文一样会被拼接吃掉(脚注不以句末标点结尾
#: 时,下一段会直接粘上来),也可能被「页眉页脚重复行剔除」当成重复装饰(如多页重复的
#: `① 同上`)。这类行一律按结构行保护。
NOTE_LINE_RE = re.compile(r"^[①-⑳]")


def _is_catalog_entry(line: str) -> bool:
    """目录/索引条目行(以页码结尾)。这类行是**独立条目**,不参与拼接与文字重写。"""
    s = line.strip()
    return bool(s) and bool(ENTRY_TAIL_RE.search(s))


def _is_note_line(line: str) -> bool:
    """注释/编号行(圈码序号开头)。"""
    return bool(NOTE_LINE_RE.match(line.strip()))


def _is_structure_line(line: str) -> bool:
    """标题行 / 目录条目行 / 注释行:内部空格与分行都是有意的排版结构。

    这类行不做「空格重写」类清理:删掉 `第一章 巴勒斯坦问题的由来` 里的空格,会把
    序号与标题粘成一个词(真实产物上实测如此),而那个空格是排版者写的分隔。
    """
    s = line.strip()
    return bool(HEADING_RE.match(s)) or _is_catalog_entry(s) or _is_note_line(s)


def _looks_like_layout_line(line: str) -> bool:
    """版式字段行:不以句末标点结尾,且**短**(≤ 20 字)或**含数字/拉丁字符**。

    正文段落总以标点收尾;没有标点又独占一段的行主要是排版出来的「一行一个字段」
    (`人民出版社出版 新華書店发行`、`787×1092毫米32开本 2.25印张 43.000字`、
    `书号 3001·1505 定价 0.15 元`)。短、或夹着数字/型号,都是字段的特征;
    一长串纯中文而无标点则更可能是被断开的正文(那种行允许参与拼接)。
    """
    s = line.strip()
    if not s or _is_block_start(s):
        return False
    if s[-1] in END_PUNCT:
        return False
    visible = re.sub(r"\s+", "", s)
    return len(visible) < LAYOUT_MAX_CHARS or bool(re.search(r"[0-9A-Za-z]", visible))


def _is_layout_unit(kind: str, text: str, gap: int) -> bool:
    """拼接判定用的版式字段行判据:普通文本、独占一段(前面有空行)、且像字段行。

    字段行**不参与拼接**(两个方向都不):它是排版的一条独立信息,
    后面紧跟的往往是另一条信息而不是它的下半句。
    """
    return kind == "text" and gap >= 1 and _looks_like_layout_line(text)


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


def _keep_line_break(line: str) -> bool:
    """该行需要靠硬换行保住分行:诗行,**或非块结构的目录条目行 / 注释行**。

    条目行与注释行常常比诗行更长(实测目录条目 19 字),但 pandoc 会把段落里的软换行
    渲染成空格 —— 不补硬换行,它们会在阅读器里挤成一行(修复前甚至直接被拼成一行)。
    页脚脚注集中排布时也是这种形态(`① …`/`② …` 连续多行)。
    已经是块结构的行(`1. 文学社/3`)由列表渲染保证独立,不必补硬换行。
    """
    if _is_verse_line(line):
        return True
    s = line.strip()
    return bool(s) and not _is_block_start(s) and (_is_catalog_entry(s) or _is_note_line(s))


def _is_running_head_candidate(s: str, max_len: int) -> bool:
    """页眉页脚候选行:短、无句末标点、非 markdown 块结构。

    页眉页脚(书名/章节名/页码装饰)在正文里反复出现且**不带句末标点**;
    正文句子几乎总以标点结尾,列表/表格/标题/代码有块标记,都会被排除。
    """
    if not s or len(s) > max_len:
        return False
    if _is_block_start(s) or "[" in s or "]" in s:
        return False
    if _is_catalog_entry(s) or _is_note_line(s):
        return False                        # 目录/索引条目与注释是有内容的行,不是页面装饰
    if s[-1] in END_PUNCT:
        return False
    if s.isdigit():
        return False                        # 纯页码交给 page_numbers
    return any(ch.isalnum() or CJK_RE.match(ch) for ch in s)


# ---------------------------------------------------------------- 页码行

#: 纯数字页码行(1-3 位)。4 位以上是年份/编号,不当页码。
PAGE_NUMBER_RE = re.compile(r"^[ \t]*\d{1,3}[ \t]*$")
#: 中文形态页码行(`第 3 页` / `— 3 —`):PyMuPDF 把页脚页码提取成这种样子,
#: 只认纯数字会漏掉,成品里每页中间就夹着一行页码。
PAGE_NUMBER_CJK_RE = re.compile(
    r"^[ \t]*(?:第\s*\d{1,4}\s*页|[—–-]\s*\d{1,4}\s*[—–-])[ \t]*$")


def is_page_number_line(line: str) -> bool:
    """整行是不是一个页码(两种形态之一)。"""
    s = line.rstrip("\n")
    return bool(PAGE_NUMBER_RE.match(s) or PAGE_NUMBER_CJK_RE.match(s))
