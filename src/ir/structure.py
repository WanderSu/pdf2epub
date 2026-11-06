"""按**证据层**裁决标题:补回 md 里丢掉的章节标题,降级没有出处的伪标题。

为什么需要这一层:标题是唯一「证据层有、正文没有」会静默成灾的结构事实。后端版面
模型把章标题判成 ``header``(页眉)时,它生成的 Markdown 里**一行都不写**;而引擎只
拿后端的 Markdown 当正文源、把 ``content_list`` 当只读证据 —— 于是标题在第一步就
没了。清理器看到的是一份「本来就没有标题」的 md:页账自洽、内容对照自洽、报告零
告警,读者看到的却是一本只有一个大文件的书。

反向的错法同样常见:后端把一句对白、一个裸数字标成标题,md 里就多出
``## “您在干吗呢?”`` / ``## 2`` 这种条目,直接污染 EPUB 导航。

所以这里只做两件事,判据都来自**证据层**(文本类型 + 版面坐标 + 目录页),不猜语义:

1. **补**:证据层是标题类的块、md 里又没有同名标题的,按证据的页序找到 md 的落点
   插入;证据层没给、但目录页给了(``标题 / 页码`` 连续 ≥3 行)且正文里已有这一行
   独立成段的,就地提升。
2. **降**:md 里的标题若证据层说是普通正文/页码,或形态上不可能是标题(纯数字、
   整句引语、超长行),或证据层自己在**页面中部**标了它却是一句话 —— 降回段落。

四条「不猜」的线:

- 同名页眉出现在 ≥3 个页面上 → 按书眉处理,不当标题(目录页也列了它的除外);
- 标题来源少于 ``_MIN_TITLE_SOURCES`` 条时**不降级**:整本书都没有标题信息时,
  「无法证明是标题」不等于「该删」;
- 只有 1 个字的标题只接受**显式来源**(证据层标题类 / 目录页),不从正文段落里提升;
- 落点找不到就不动,在 notes 里点名,让调用方把这句话带给用户。

只改标题行本身(插入 ``# `` / 去掉 ``# ``),不动任何正文,因此不影响往返一致。
"""
from __future__ import annotations

import re
from typing import Sequence

from ir.model import Block
from markdown.textrules import (_is_block_start, _is_catalog_entry, _is_page_mark,
                                is_page_number_line)

#: 标题文本的长度上限(归一化后):超过这个长度基本是一句话,不是标题
_TITLE_MAX = 40

#: 从正文段落提升标题时的最小长度(1 个字的匹配没有判别力)
_PROMOTE_MIN_CHARS = 2

#: 结构性标签:它们确实是标题,但不是「章」。列进目录只会让导航多一条无意义的项
_STRUCT_LABELS = frozenset({"目录", "contents", "tableofcontents", "目次"})

#: 连续多少行 ``标题 / 页码`` 才算「这一页是目录」——单行可能是正文里的引用
_TOC_MIN_RUN = 3

#: 标题来源少于这个数时不做降级(证据太少时宁可不动)
_MIN_TITLE_SOURCES = 3

#: 同名页眉出现在这么多个页面上,就按书眉处理(不是标题)
_RUNNING_HEAD_PAGES = 3

#: 「页面顶部」的相对高度上限:章标题从页首开始,页中部的同类块是句中的强调/对白
_TOP_OF_PAGE = 0.25

#: 句末标点:标题极少以此结尾,而句子几乎总是
_SENTENCE_END = "。！？!?…"

#: 定位用的锚点长度:取该页第一条正文的这一段前缀,用它反查 md 里的落点
_ANCHOR_CHARS = 24

#: 锚点最短长度:章首句可能很短(实测「老金见到那个孩子是在一个夏夜。」只有 16 字),
#: 门槛定高了会把整段跳过,标题就落到第二段之后 —— 也就是「标题前还漏一段」
_ANCHOR_MIN_CHARS = 4

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*\S)[ \t]*$")

#: 目录条目的「标题 … 页码」尾部(页码可以是阿拉伯数字或罗马数字)
_TOC_TAIL_RE = re.compile(
    r"[ \t]*(?:[/／]|\.{2,}|…+|·{2,}|[ \t]{2,})[ \t]*"
    r"(?P<page>[0-9]{1,4}|[ivxlcdmIVXLCDM]{1,7})[ \t]*$"
)

#: 全角标点折叠成 ASCII:目录页与正文常常一份全角一份半角(``附录：`` vs ``附录:``)
_PUNCT_FOLD = str.maketrans({
    "：": ":", "，": ",", "。": ".", "、": ",", "；": ";", "！": "!",
    "？": "?", "（": "(", "）": ")", "／": "/", "《": "<", "》": ">",
    "「": '"', "」": '"', "『": '"', "』": '"', "“": '"', "”": '"',
    "‘": "'", "’": "'", "—": "-", "－": "-",
})

#: 证据层里「这是标题」的类型
_TITLE_KINDS = frozenset({"heading", "header"})

#: 只承载页界/装饰、不参与定位的证据类型
#: ``footnote`` 也跳过:页脚注释常被 OCR 排到下一页的页首,拿它当锚点会把章标题
#: 插到"上一章的脚注"之后(实测一本扫描书:章标题落到了归属上一章的注文下面)。
_ANCHOR_SKIP_KINDS = frozenset({"page_number", "header", "footer", "footnote", "unknown"})


def _norm(text: str) -> str:
    """去掉所有空白:目录与正文之间只差排版空格,比对时不看它们。"""
    return re.sub(r"\s+", "", text)


def _key(text: str) -> str:
    """比对键:去空白 + 全角标点折叠 + 去掉首尾装饰符号。"""
    return _norm(text).translate(_PUNCT_FOLD).strip("-\"'.,:;<>()*_#").lower()


def _sentence_like(text: str) -> bool:
    """像一句话而不像标题:整句被引号包住,或以句末标点收尾且很短。"""
    norm = _norm(text)
    if not norm:
        return False
    if norm[0] in "\"“‘「『" and norm[-1] in "\"”’」』":
        return True
    return norm[-1] in _SENTENCE_END and len(norm) <= 15


def _never_a_title(text: str) -> bool:
    """两种「几乎不可能是章标题」的形态:整句引语、裸数字。

    即使证据层自己标成 heading 也算不上:后端版面模型把强调句、对白判成标题是常见
    误判,而它们出现在导航里比「标题少一条」更刺眼。
    """
    norm = _norm(text)
    if not norm:
        return True
    if norm.isdigit() and len(norm) <= 3:
        return True
    return norm[0] in "\"“‘「『" and norm[-1] in "\"”’」』"


def _impossible_title(text: str) -> bool:
    """形态上不可能是标题:纯数字、整句引语、超长行。"""
    norm = _norm(text)
    if not norm:
        return True
    return _never_a_title(text) or len(norm) > _TITLE_MAX


def _clean_title(text: str) -> str:
    """标题原样使用,只把折行/多空格压成一个空格。"""
    return re.sub(r"\s+", " ", text).strip()


class _Title:
    """一个待裁决的标题:名称 + 证据层序号(定位用)+ 只有目录页时的印刷页码。"""

    __slots__ = ("key", "text", "ev_idx", "page")

    def __init__(self, key: str, text: str, ev_idx: int | None = None,
                 page: int | None = None) -> None:
        self.key = key
        self.text = text
        self.ev_idx = ev_idx
        self.page = page


# ---------------------------------------------------------------- 版面

def _page_heights(evidence: Sequence[Block]) -> dict[int, float]:
    """每页「出现过的最低边」—— 没有页高字段时,用它近似页面高度。"""
    heights: dict[int, float] = {}
    for block in evidence:
        box = block.prov.bbox
        if block.page_idx is None or not box:
            continue
        heights[block.page_idx] = max(heights.get(block.page_idx, 0.0), float(box[3]))
    return heights


def _page_top(block: Block, heights: dict[int, float]) -> bool | None:
    """这块是否落在页面顶部;判断不出来(没有坐标/页高)时返回 None。"""
    box = block.prov.bbox
    height = heights.get(block.page_idx) if block.page_idx is not None else None
    if not box or not height:
        return None
    return float(box[1]) / height <= _TOP_OF_PAGE


# ---------------------------------------------------------------- 索引

def _index_evidence(evidence: Sequence[Block]) -> tuple[dict[str, _Title], set[str]]:
    """证据层 → (标题候选, 页中部被判成标题的键)。

    **不看**「这条文本在证据里是普通正文」:书名页、扉页上的书名/作者在证据层里同样
    是 ``text``,拿它当"该标题是假的"的证据会把书名标题一起降级(实测一本政经读物:
    ``# 工资、价格和利润`` 被降成段落,整本书失去唯一的一级标题)。
    """
    titles: dict[str, _Title] = {}
    mid_page: set[str] = set()
    heights = _page_heights(evidence)
    for idx, block in enumerate(evidence):
        key = _key(block.text)
        if not key:
            continue
        if block.kind not in _TITLE_KINDS:
            continue
        text = block.text.strip()
        top = _page_top(block, heights)
        # ``header`` 是「这块在页眉位置」的版面判定,位置已经由后端给出;
        # ``heading`` 只说明层级,位置要自己核:页中部的 heading 多半是句中的强调。
        if block.kind == "heading" and top is False:
            mid_page.add(key)
            continue
        if key in titles or not _title_like(block):
            continue
        titles[key] = _Title(key, _clean_title(text), idx, block.page_idx)
    return titles, mid_page


def _title_like(block: Block) -> bool:
    """这条证据像不像**标题**(而不是页眉/页码/整段正文)。"""
    text = block.text.strip()
    key = _norm(text)
    if not key or len(key) > _TITLE_MAX:
        return False
    if is_page_number_line(text) or _is_catalog_entry(text):
        return False
    if _TOC_TAIL_RE.search(text):           # 「译序 / i」这类条目形态(罗马数字页码)
        return False
    if _is_block_start(text):
        return False                       # md 块标记出现在证据里 = 不是标题文本
    return True


def _toc_titles(lines: Sequence[str]) -> dict[str, _Title]:
    """目录页 → 标题候选。

    只认**连续 ≥3 行**且每行都是「标题 + 页码」的块:单行 ``…/35`` 可能是正文里的
    引用,连续三行以上才是目录。
    """
    found: dict[str, _Title] = {}
    run: list[tuple[str, str]] = []

    def flush() -> None:
        for title, page in run:
            key = _key(title)
            if not _norm(title) or key in found:
                continue
            page_no = int(page) if page.isdigit() else None
            found[key] = _Title(key, _clean_title(title), None, page_no)
        run.clear()

    for line in lines:
        s = line.strip()
        if not s:
            continue
        m = _TOC_TAIL_RE.search(s)
        if m and _is_catalog_entry(s):
            run.append((s[: m.start()].strip(), m.group("page")))
        else:
            if len(run) >= _TOC_MIN_RUN:
                flush()
            else:
                run.clear()
    if len(run) >= _TOC_MIN_RUN:
        flush()
    return found


def _drop_running_heads(titles: dict[str, _Title], evidence: Sequence[Block],
                        keep: set[str]) -> None:
    """同名页眉出现在 ≥3 页上 → 是书眉,不当标题(目录页列了它的除外)。"""
    pages: dict[str, set[int]] = {}
    for block in evidence:
        if block.kind not in _TITLE_KINDS or block.page_idx is None:
            continue
        pages.setdefault(_key(block.text), set()).add(block.page_idx)
    for key, seen in pages.items():
        if key in titles and key not in keep and len(seen) >= _RUNNING_HEAD_PAGES:
            titles.pop(key, None)


# ---------------------------------------------------------------- 定位

def _reading_pos(block: Block) -> tuple[float, float]:
    """版面阅读位置的排序键(自上而下、自左而右);没有坐标的排到后面。"""
    box = block.prov.bbox
    return (float(box[1]), float(box[0])) if box else (1e9, 1e9)


def _anchors_for(title: Block, evidence: Sequence[Block]) -> list[tuple[str, bool]]:
    """该标题在正文里的候选落点,按版面阅读顺序给出(去重后的归一化前缀)。

    不能按「标题之后的下一条证据」取:后端的 ``header`` 常常排在本页所有正文之后
    (实测同一本书:章标题在块列表末尾,它下面三段正文排在它前面)。按列表顺序取,
    标题就会被插到本章前几段之后 —— 读者看到的正是「标题之前还有几段正文」。

    给出的顺序是:同页标题下方(y 不小于标题)的块 → 同页其余块 → 后 1–3 页的块,
    组内按 ``(y, x)`` 自上而下、自左而右。调用方逐个拿去 md 里找,第一个找到的
    就是落点;短首句(不足 ``_ANCHOR_CHARS`` 字)同样参与,否则会整段被跳过。
    """
    page, box = title.page_idx, title.prov.bbox

    def usable(block: Block) -> tuple[str, bool] | None:
        if block.kind in _ANCHOR_SKIP_KINDS:
            return None
        key = _norm(block.text)
        if len(key) < _ANCHOR_MIN_CHARS:
            return None
        # 短块必须整行相等:它的文字很可能出现在别处的长段落里(实测:前言里引用了
        # 小说首句,按"包含"匹配会把章标题插进前言)
        return key[:_ANCHOR_CHARS], len(key) <= _ANCHOR_CHARS

    out: list[tuple[str, bool]] = []
    if page is None:
        # 没有页号:只能退回列表顺序(此时宁可晚一点也不乱插)
        start = evidence.index(title) + 1 if title in evidence else 0
        for block in evidence[start:]:
            anchor = usable(block)
            if anchor:
                out.append(anchor)
        return out

    same = [b for b in evidence if b.page_idx == page and usable(b)]
    below = [b for b in same
             if not box or not b.prov.bbox or float(b.prov.bbox[1]) >= float(box[1]) - 2.0]
    groups = [sorted(below, key=_reading_pos), sorted(same, key=_reading_pos)]
    for step in range(1, 4):
        groups.append(sorted([b for b in evidence
                              if b.page_idx == page + step and usable(b)],
                             key=_reading_pos))
    for group in groups:
        for block in group:
            anchor = usable(block)
            if anchor and anchor not in out:
                out.append(anchor)
    return out


def _find_line(lines: Sequence[str], anchor: str, start: int, *,
               exact: bool = False) -> int | None:
    """从 ``start`` 起找锚点所在行(单调前进,避免同一段出现两次时错位)。

    ``exact=True`` 要求整行与锚点相等(锚点本身就是整块的完整文字):短句常常被别处
    的长段落引用,按"包含"匹配会落到引用它的那段文字里。
    """
    for pos in range(max(0, start), len(lines)):
        norm = _norm(lines[pos])
        if (norm == anchor) if exact else (anchor in norm):
            return pos
    return None


def _heading_above(lines: Sequence[str], pos: int, key: str) -> bool:
    """``pos`` 正上方(跳过空行与页码注释)**紧邻**的是不是同名标题。

    只认紧邻:同名标题出现在更远的位置时,它八成是书名页上的书名或被正文引用的同一句,
    不是本章的章标题 —— 那种情况下要补一个新的,旧的随后会被降级。
    """
    for j in range(pos - 1, -1, -1):
        s = lines[j].strip()
        if not s or _is_page_mark(s):
            continue
        m = _HEADING_RE.match(s)
        return bool(m) and _key(m.group(2)) == key
    return False


def _standalone_line(lines: Sequence[str], key: str) -> int | None:
    """正文里**独立成段**且整行就是这个标题的行号(用于就地提升)。

    前面必须是空行(它自成一段);后面可以是空行,也可以是一行**短且不带句末标点**
    的署名式内容 —— 实测常见形态是标题下面紧跟作者名,把这种也算独立成段,否则
    目录里有、证据层没给的标题永远升不起来。
    """
    for pos, line in enumerate(lines):
        s = line.strip()
        if not s or _HEADING_RE.match(s) or _is_catalog_entry(s):
            continue
        if _key(s) != key:
            continue
        before = lines[pos - 1].strip() if pos > 0 else ""
        after = lines[pos + 1].strip() if pos + 1 < len(lines) else ""
        if before:
            continue
        if not after or (len(_norm(after)) <= 20 and after[-1] not in "。！？.!?；;"):
            return pos
    return None


# ---------------------------------------------------------------- 主入口

def restore_headings(md: str, evidence: Sequence[Block], *,
                     title_hint: str | None = None) -> tuple[str, tuple[str, ...]]:
    """按证据层补回/降级标题,返回 ``(md, notes)``。

    ``title_hint`` 只用来排除「第 1 页上的书名」被当成章标题(封面页由
    ``front_matter`` 规则处理);其余判断都不看它,免得把语义猜测引进来。
    """
    if not evidence:
        return md, ()

    lines = md.split("\n")
    ev_titles, mid_page = _index_evidence(evidence)
    toc = _toc_titles(lines)
    hint_key = _key(title_hint) if title_hint else ""

    titles: dict[str, _Title] = dict(ev_titles)
    for key, entry in toc.items():
        if key in titles:
            # 目录页给的是印刷标题,优先用它(证据层那一行可能是被拆开的)
            titles[key].text = entry.text
        else:
            titles[key] = entry
    _drop_running_heads(titles, evidence, keep=set(toc))

    existing = {_key(m.group(2)) for m in map(_HEADING_RE.match, lines) if m}
    struct_keys = {_key(label) for label in _STRUCT_LABELS}
    inserted = promoted = 0
    missing: list[str] = []
    cursor = 0

    for entry in sorted(titles.values(),
                        key=lambda t: t.ev_idx if t.ev_idx is not None else 10 ** 9):
        if entry.key in struct_keys:
            continue
        if hint_key and entry.key == hint_key and (entry.page or 0) == 0:
            continue                        # 第 1 页上的书名不是章标题
        pos = None
        if entry.ev_idx is not None:
            for anchor, exact in _anchors_for(evidence[entry.ev_idx], evidence):
                pos = _find_line(lines, anchor, cursor, exact=exact)
                if pos is not None:
                    break
        if pos is not None and entry.key in existing and _heading_above(lines, pos, entry.key):
            cursor = pos                      # 章首已经有这个标题,不重复插
            continue
        if pos is None:
            if len(_norm(entry.text)) < _PROMOTE_MIN_CHARS:
                missing.append(entry.text)
                continue
            hit = _standalone_line(lines, entry.key)
            if hit is None:
                missing.append(entry.text)
                continue
            lines[hit] = f"# {lines[hit].strip()}"
            existing.add(entry.key)
            promoted += 1
            cursor = max(cursor, hit)
            continue
        if pos > 0 and lines[pos - 1].strip():
            lines.insert(pos, "")
            pos += 1
        lines.insert(pos, f"# {entry.text}")
        lines.insert(pos + 1, "")
        existing.add(entry.key)
        inserted += 1
        cursor = pos + 2

    # 同名标题只留**章首那一个**:md 开头常有 ``# 书名``(扉页/版权页),同名短篇集
    # 又会让章标题与它同名 —— pandoc 的 section id 相同,nav 里两条目录项会指向同一处
    # (实测:第二个「烧纸」跳回书名页)。章在书里出现得比书名页晚,所以留最后一个。
    for key in titles:
        spots = [pos for pos, line in enumerate(lines)
                 if (m := _HEADING_RE.match(line)) and _key(m.group(2)) == key]
        for pos in spots[:-1]:
            lines[pos] = _HEADING_RE.match(lines[pos]).group(2).strip()

    # 章标题统一到最外层:目录/证据点名的标题若还带着 ``##``(后端标的层级),pandoc
    # 会把它挂到上一章下面 —— 导航里就成了子项,按章切分也跟着错。
    levels = [len(m.group(1)) for m in map(_HEADING_RE.match, lines)
              if m and _key(m.group(2)) in titles]
    base = min(levels) if levels else 0
    if base:
        for pos, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m or _key(m.group(2)) not in titles:
                continue
            if len(m.group(1)) != base:
                lines[pos] = "#" * base + " " + m.group(2)

    demoted: list[str] = []
    if len(titles) >= _MIN_TITLE_SOURCES:
        toc_keys = set(toc)
        for pos, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m:
                continue
            text = m.group(2).strip()
            key = _key(text)
            if key in toc_keys:
                continue                       # 目录点名 = 真章名,不降
            if key in titles and not _never_a_title(text):
                continue                       # 证据层背书
            if _impossible_title(text) or (key in mid_page and _sentence_like(text)):
                lines[pos] = text
                demoted.append(text[:24])

    notes: list[str] = []
    if inserted or promoted or demoted:
        notes.append(
            f"标题裁决:{len(titles)} 个标题来源,补回 {inserted} 个、提升 {promoted} 个、"
            f"降级 {len(demoted)} 个无出处标题"
            + (f"(如 {'、'.join(demoted[:3])})" if demoted else "")
        )
    if missing:
        notes.append(
            f"标题裁决:{len(missing)} 个标题在正文里找不到落点,未补:"
            + "、".join(missing[:4])
        )
    return "\n".join(lines), tuple(notes)


__all__ = ["restore_headings"]
