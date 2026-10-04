"""Markdown 清理模块(idea.md §7)。

原则:修复结构,不改写正文;不用 LLM 重写。
当前实现:
  - 统一换行符(CRLF → LF)
  - 围栏代码块 / 行内代码 / 脚注定义块**掩码**:规则链全程不触碰它们,结束时逐字节还原
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
3. **脚注定义块(`[^1]: …`)是不可改写的结构**。它在清理器眼里只是一行普通文本,
   但 pandoc 认的是这个语法:一旦被拼进正文段、被补硬换行、或者续行的缩进被 strip,
   整条脚注就降级成正文里的普通文字(且**源里不再有 `[^1]:`**,内容对照校验看不见)。
   与代码块同理,靠规则各自「跳过」是拦不住的,所以整体掩码。
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
#: 行内 CJK 字符(判断「中文语境」)
CJK_RE = re.compile(rf"[{CJK_CHARS}]")

#: 目录 / 索引条目行的行尾:页码(可带 `/`、点线、省略号、`·` 或空格引导)。
#: 真实书籍的目录就是这种形态(`第一章 失踪与疯癫/35`、`第五章……204`),而它**没有
#: 句末标点**、长度也常常超过诗行门槛 —— 修复前这类行会被 `join_lines` 拼成一整行
#: (实测三条目录章节被粘成 `第一章…/1第二章…/15第三章…/32`)。
ENTRY_TAIL_RE = re.compile(r"(?:/|\.{2,}|…+|·{2,}|\s|^)\s*\d{1,4}\s*$")


def _is_catalog_entry(line: str) -> bool:
    """目录/索引条目行(以页码结尾)。这类行是**独立条目**,不参与拼接与文字重写。"""
    s = line.strip()
    return bool(s) and bool(ENTRY_TAIL_RE.search(s))


#: 注释/编号行:以 ①-⑳ 这类圈码序号开头。
#: 页脚脚注在产物里就是这个形态(`① 列宁:《告犹太工人书》…`),MinerU 补回的页脚脚注
#: 也正是这种独立段落 —— 它们和正文一样会被 `join_lines` 吃掉(脚注不以句末标点结尾
#: 时,下一段会直接粘上来),也可能被「页眉页脚重复行剔除」当成重复装饰(如多页重复的
#: `① 同上`)。这类行一律按结构行保护。
NOTE_LINE_RE = re.compile(r"^[①-⑳]")


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


def _map_body_lines(md: str, fix) -> str:
    """只对**正文行**跑 fix;结构行与「独立成段的版式行」原样保留。

    两类行被跳过(见 ``_is_structure_line`` 与 ``_looks_like_layout_line``):
      - 标题行 / 目录条目行 / 注释行 —— 它们的空格与分行是有意的结构;
      - 独立成段、又不以句末标点结尾的行 —— 版权页那种「一行一个字段」的版式
        (`人民出版社出版 新華書店发行`、`书号 3001·1505 定价 0.15 元`)。真实扫描书
        实测:全篇只有这几行的空格被删除,删完两个字段就粘成一个词。
    """
    lines = md.split("\n")

    def _blank(index: int) -> bool:
        if not (0 <= index < len(lines)):
            return True
        s = lines[index].strip()
        return s == "" or _is_page_mark(s)

    out: list[str] = []
    for i, line in enumerate(lines):
        isolated = _blank(i - 1) and _blank(i + 1)
        skip = (
            _is_structure_line(line)              # 标题 / 目录条目 / 注释
            or _is_verse_line(line)               # 短行且无句末标点(诗句、短标签、标题)
            or (isolated and _looks_like_layout_line(line))   # 独立成段的版式行(版权页)
        )
        out.append(line if skip else fix(line))
    return "\n".join(out)

#: 围栏代码块标记(``` 开头即进入 / 退出代码块),与 markdown 惯例一致
FENCE_MARK = "```"
#: 行内代码:同一行内成对的单反引号片段(跨行、配不成对的一律不动)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
#: 占位符里用的哨兵字符:不可能出现在正常 Markdown 文本里
SENTINEL = "\x00"
#: 还原后仍残留的占位符(说明有规则改写了它 —— 那意味着受保护内容可能已损坏)
MASK_LEFTOVER_RE = re.compile(rf"{SENTINEL}[fcn]\d+{SENTINEL}")

#: 脚注定义行:`[^label]: 内容`(最多 3 个前导空格,与 pandoc 的块级缩进一致)
FOOTNOTE_DEF_RE = re.compile(r"^ {0,3}\[\^[^\]\s]+\]:")
#: 脚注续行的缩进(**pandoc 只把缩进 ≥4 空格/tab 的行算作脚注内容**)
FOOTNOTE_CONT_INDENT_RE = re.compile(r"^(?: {4,}|\t)")


def _is_footnote_cont(line: str) -> bool:
    """该行是否属于上一条脚注定义的续行(缩进足够且非空)。"""
    return bool(line.strip()) and bool(FOOTNOTE_CONT_INDENT_RE.match(line))


def _mask_token(blocks: list[tuple[str, str]], kind: str, text: str,
                wrapper: str = "") -> str:
    """登记一块受保护原文并返回占位符(kind: f=围栏代码 / c=行内代码 / n=脚注)。

    wrapper 用 `<` 把占位符包成「markdown 块结构」:拼接规则既不会吃掉它,
    也不会把相邻正文拼进它。
    """
    ph = f"{wrapper}{SENTINEL}{kind}{len(blocks)}{SENTINEL}{wrapper}"
    blocks.append((ph, text))
    return ph


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
        return _mask_token(blocks, kind, text, wrapper)

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


def _mask_footnotes(md: str, blocks: list[tuple[str, str]]) -> str:
    """把脚注定义块(定义行 + 缩进续行)整体掩码,保证清理规则不改写它们。

    为什么必须保护:`[^1]: …` 在清理规则眼里就是一行普通文本 ——
      - `_join_broken_lines` 会把紧跟正文的脚注定义**拼进上一段**(上一行没有句末
        标点时必然发生,而 OCR 产物的行尾恰恰常常没有句点):定义语法就此消失;
      - 多条定义连排时,后一条会被拼进前一条的内容里(pandoc 里引用变成未定义);
      - 续行的缩进会被 strip、短脚注会被 `_mark_verse_lines` 补上硬换行。

    实测后果:同一份 Markdown 清理前 `footnote_refs=2`,清理后 `footnote_refs=0` ——
    脚注整批降级成正文流文字;而内容对照校验查不出来(源里已经没有 `[^1]:` 可对了)。

    保护范围与 pandoc 的脚注语法一致(定义行 + 缩进 ≥4 空格的续行),不含空行分隔
    的下一段正文(缩进不足的行 pandoc 本来也不算脚注内容)。
    """
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not FOOTNOTE_DEF_RE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines):
            if _is_footnote_cont(lines[j]):
                j += 1
                continue
            if not lines[j].strip():
                # 空行后仍跟着缩进续行时,空行属于这条脚注(多段脚注)
                k = j
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and _is_footnote_cont(lines[k]):
                    j = k
                    continue
            break
        out.append(_mask_token(blocks, "n", "\n".join(lines[i:j]), wrapper="<"))
        i = j
    return "\n".join(out)


def _unmask_protected(md: str, blocks: list[tuple[str, str]]) -> str:
    """还原受保护原文(脚注块 / 围栏代码 / 行内代码)。还原不完整即报错,不静默丢内容。

    **逆序还原**:脚注块里可能嵌着行内代码占位符,先还原脚注、再还原代码 ——
    顺序反了会留下未还原的占位符,进而被误判成「有规则改写了受保护内容」。

    只检查**占位符形状**而不是「文本里有没有哨兵字符」:输入文件本身带 NUL 时
    不该让整本书转换失败(那是上游的问题,不是清理器的)。
    """
    for placeholder, original in reversed(blocks):
        md = md.replace(placeholder, original)
    if MASK_LEFTOVER_RE.search(md):
        raise RuntimeError("清理器占位符还原失败(有规则改写了受保护内容)")
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


def _mark_verse_lines(md: str, report: CleanReport) -> str:
    """给连续短行(诗行)与目录条目行加 Markdown 硬换行,免得它们被渲染成一行。

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
        if not _keep_line_break(lines[i]):
            i += 1
            continue
        run = [i]                                      # 连续诗行 / 条目行的行号
        cursor = i
        while True:
            k = cursor + 1
            while k < len(lines) and _is_page_mark(lines[k]):
                k += 1                                 # 页码注释透明
            if k < len(lines) and _keep_line_break(lines[k]):
                run.append(k)
                cursor = k
            else:
                break
        if len(run) >= 2:                              # 连续 ≥2 行才当诗/条目组
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

    # 1.6 脚注定义块掩码:同理由,且必须与代码块同一套机制 —— 脚注被拼进正文后
    #     pandoc 就再也认不出它,内容对照校验也查不出来(源里没有 `[^1]:` 可对了)
    md = _mask_footnotes(md, code_blocks)

    # 2. 剔除页码残留:独立纯数字行(1-3 位,前后可有空白)
    if options.page_numbers:
        md = re.sub(r"(?m)^[ \t]*\d{1,3}[ \t]*$\n?", "", md)

    # 3. 剔除页眉页脚重复行(必须在跨页拼接之前:否则页眉会被拼进正文)
    if options.running_heads:
        md = _strip_running_heads(md, report)

    # 4. 中文排版空格:中文(含中文标点)之间、中文与数字之间的空格
    #    (保留中英之间的空格;标题与目录条目行不动 —— 那里的空格是排版结构)
    if options.cjk_spaces:
        md = _fix_cjk_spaces(md)

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

    # 10.8 还原受保护内容(必须排在所有规则之后:原文逐字节回写)
    md = _unmask_protected(md, code_blocks)

    # 11. 图片引用存在性校验
    if options.images and images_dir is not None and images_dir.is_dir():
        existing = {p.name for p in images_dir.iterdir() if p.is_file()}
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", md):
            ref = m.group(1)
            name = Path(ref).name
            if name not in existing and not Path(ref).is_absolute():
                report.add(f"图片引用缺失: {ref}")

    return md


def _fix_cjk_spaces(md: str) -> str:
    """中文排版空格修正:汉字/中文标点/数字之间、半角括号与内联 HTML 标签两侧。

    只对**正文行**生效(见 ``_is_structure_line``):
      - `第一章 巴勒斯坦问题的由来` 这类标题里的空格是序号与标题的分隔;
      - `第三章 武装斗争/32` 这类目录条目里的空格同理。
    删掉它们会把两个字段粘成一个词 —— 这是「改写结构」而不是「清理垃圾」。
    """
    def fix(line: str) -> str:
        line = re.sub(rf"(?<=[{CJK_CHARS}]) (?=[{CJK_CHARS}])", "", line)
        line = re.sub(rf"(?<=[{CJK_CHARS}]) (?=\d)", "", line)
        line = re.sub(rf"(?<=\d) (?=[{CJK_CHARS}])", "", line)
        # 内联 HTML 标签两侧(如 "<u>首" 前的空格)与半角括号两侧
        line = re.sub(rf"(?<=[{CJK_CHARS}]) (?=<[^>]+>)", "", line)
        line = re.sub(r"(</?[a-zA-Z]{1,5}>) (?=[\u4e00-\u9fff])", r"\1", line)
        line = re.sub(rf"(?<=[{CJK_CHARS}]) (?=[()])", "", line)
        line = re.sub(rf"(?<=[()]) (?=[{CJK_CHARS}])", "", line)
        return line

    return _map_body_lines(md, fix)


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

    标题行与目录条目行不参与(与 ``_fix_cjk_spaces`` 同一取舍:那里的空格是结构)。
    """
    frag = rf"[A-Za-z]{{1,{max_frag}}}"
    pattern = re.compile(rf"(?<![A-Za-z]){frag}(?: {frag}){{{min_frags - 1},}}(?![A-Za-z])")
    hits = 0

    def fix_line(line: str) -> str:
        nonlocal hits
        if not CJK_RE.search(line) or _is_structure_line(line):
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

    **但「没有句末标点」本身不是断行的证据**(它只是必要条件):目录/索引条目
    (`第一章 失踪与疯癫/35`、`第五章……204`)同样没有句末标点,却是有意的独立行 ——
    它们现在被排除在拼接之外(``_is_catalog_entry``)。宁可少拼一处,也不要把两行
    独立内容粘成一行。

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
    #: pending 这一行是否「版式字段行」(独占一段、无句末标点、短或含数字/拉丁字符)。
    #: 两个相邻的字段行**不许拼接** —— 它们是并列字段(版权页),不是被断开的句子;
    #: 单个字段行仍可能与下一段构成一次真正的断行拼接,所以只在两侧都是字段行时禁拼。
    pending_layout = False

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
            pending_layout = _is_layout_unit(kind, text, blanks)
            blanks = 0
            continue
        pk, pt, pb = pending
        gap = blanks
        prev_len = len(re.sub(r"\s+", "", pt))
        cur_len = len(re.sub(r"\s+", "", text))
        # 诗行对:两行都短、且**两行都没有句末标点** —— 换行是有意的,不能拼。
        # (JOIN_MIN_CHARS=20 之后,「两行都短」这一半已被长度门槛覆盖;保留这个条件是为了
        #  两个判据始终一致 —— 「不拼接」与「补硬换行」必须用同一个门槛,否则改回低门槛时
        #  13-18 字的诗行会既不拼接、又拿不到硬换行,在阅读器里照样挤成一行。)
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
            # 目录/索引条目行与注释行是独立结构,两侧都不参与拼接
            # (见 `_is_catalog_entry` / `_is_note_line`)
            and not _is_catalog_entry(pt)
            and not _is_catalog_entry(text)
            and not _is_note_line(pt)
            and not _is_note_line(text)
            and pt[-1] not in END_PUNCT
            and text[0] not in START_PUNCT
            # 短行不拼(诗句/年份/页眉/小标题):长度门槛见 JOIN_MIN_CHARS
            and prev_len >= (CROSS_BLANK_MIN_CHARS if gap >= 1 else ADJACENT_MIN_CHARS)
            # 版式字段行不参与拼接(两个方向):这类行是排版上的独立信息 ——
            # 版权页实测被粘成过一整行,图片说明/小节标题也被吞进过正文
            and not pending_layout
            and not _is_layout_unit(kind, text, gap)
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
            pending_layout = False             # 拼出来的段落不再是版式字段行
        else:
            out.append(pending)
            out.extend(("mark", t, g) for t, g in marks)
            marks = []
            pending = (kind, text, gap)
            pending_layout = _is_layout_unit(kind, text, gap)
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
