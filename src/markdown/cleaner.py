"""Markdown 清理模块(idea.md §7)。

原则:修复结构,不改写正文;不用 LLM 重写。
当前实现:
  - 统一换行符(CRLF → LF)
  - 页码残留剔除(独立纯数字行 1-3 位)
  - 页眉页脚重复行剔除(跨页反复出现的短行,扫描书收益最大)
  - 跨页断行连接(被页码/页脚隔断或非标点结尾的连续段落)
  - OCR 异常空格合并(中文语境里被拆开的拉丁词,如 `Py Mu PDF`)
  - 中文排版空格修正(汉字-汉字、汉字-数字之间的空格)
  - 多余空行压缩(连续 ≥3 个空行 → 1 个)
  - 重复标题去重(相邻同名标题)/ 空标题删除 / 标题层级跳跃修正
  - 行尾空白清理
  - 图片引用存在性校验
各项可用 CleanOptions 单独开关(配置 clean: 段 / CLI --clean-disable)。

新增启发式规则的共同原则:**默认保守 + 可单独关闭 + 配「不该改的例子」测试**。
不确定的改动一律不做(宁可留下噪声,也不破坏正文)。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# 段落结尾标点:以此结尾的段落视为完整段落,不参与跨页拼接
END_PUNCT = set("。！？；：、，·…—”』」）】》%％\"'")
# 段落开头标点:以此开头的段落不与上一段拼接
START_PUNCT = set("“‘『「（【《〈\"'")
# markdown 块级标记:不参与拼接
BLOCK_MARKERS = ("#", ">", "|", "- ", "* ", "```", "<", "![", "+ ", "1. ", "2. ")
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
#: 标题行(`#` ~ `######` + 可选空格 + 文本)
HEADING_RE = re.compile(r"^(#{1,6})\s*(.*)$")
#: 行内 CJK 字符(判断「中文语境」)
CJK_RE = re.compile(rf"[{CJK_CHARS}]")


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
    if s.startswith(BLOCK_MARKERS) or "[" in s or "]" in s:
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

    不拼接的单元之间**还原原有空行数**:旧实现统一按一个空行重建,会把
    紧凑列表与引用块的连续行拆成松散段落(渲染出多余段距),属于无谓的结构改写。
    """
    lines = md.split("\n")
    units: list[tuple[str, str]] = []  # (kind, text),kind: code/table/text

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
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
    blanks = 0

    for kind, text in units:
        if kind == "text" and text == "":
            blanks += 1
            continue
        if pending is None:
            pending = (kind, text, blanks)
            blanks = 0
            continue
        pk, pt, pb = pending
        gap = blanks
        prev_len = len(re.sub(r"\s+", "", pt))
        joinable = (
            pk == "text"
            and kind == "text"
            and gap <= 1
            and not pt.startswith(BLOCK_MARKERS)
            and not text.startswith(BLOCK_MARKERS)
            and pt[-1] not in END_PUNCT
            and text[0] not in START_PUNCT
            # 短行不拼(诗句/年份/页眉/小标题):跨空行比相邻更保守
            and prev_len >= (CROSS_BLANK_MIN_CHARS if gap >= 1 else ADJACENT_MIN_CHARS)
        )
        if joinable:
            # 中英文断行拼接:两侧均为拉丁字母时补空格,否则直接相连
            sep = ""
            if pt and text and pt[-1].isascii() and pt[-1].isalpha() \
                    and text[0].isascii() and text[0].isalpha():
                sep = " "
            pending = ("text", pt + sep + text, pb)
        else:
            out.append(pending)
            pending = (kind, text, gap)
        blanks = 0
    if pending is not None:
        out.append(pending)

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
