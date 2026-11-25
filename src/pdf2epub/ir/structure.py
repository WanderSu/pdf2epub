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

from pdf2epub.ir.model import Block
from pdf2epub.markdown.textrules import (_is_block_start, _is_catalog_entry, _is_page_mark,                                is_page_number_line)

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

#: 锚点最短长度:章首可能是很短的一行(实测小说里紧跟章标题的是个三字栏目名
#: 「生存者」),门槛定高了会整块跳过,标题就落到它下面 —— 章标题跑到小节标题之后。
#: 短锚点一律要求整行相等(见 ``usable``),不会因为短就乱配。
_ANCHOR_MIN_CHARS = 2

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*\S)[ \t]*$")

#: 卷首/卷末的独立部分:目录常常不列它们(实测「译序」只在页眉里出现),
#: 但它们是货真价实的章节级标题 —— 丢了就会和前后的正文粘成一整章
_FRONT_BACK_SECTION_RE = re.compile(
    r"^(译序|自序|代序|序言|序|前言|引言|导言|楔子|后记|后语|译后记|尾声|跋|附录|结语|致谢)")

#: 章号前缀(第X章 / 第X节 / 第X回…):同一章号只该有一个章标题
_CHAPTER_NO_RE = re.compile("^第[〇零一二三四五六七八九十百千0-9]+[章節节卷篇回]")

#: 目录条目的「标题 … 页码」尾部(页码可以是阿拉伯数字或罗马数字)
_TOC_TAIL_RE = re.compile(
    r"[ \t]*(?:[/／]|\.{2,}|…+|·{2,}|[ \t]{2,})[ \t]*"
    r"(?P<page>[0-9]{1,4}|[ivxlcdmIVXLCDM]{1,7})[ \t]*$"
)

#: 目录条目里「标题」与「作者/页码」之间的引线:目录常印成「标题……作者 页码」。
#: 只认「标题……页码」时,整页带作者列的目录一条都读不出来 —— 目录权威于是整条失效
#: (实测一本短篇集:目次 21 条一条没认出来,作者名与裸编号全成了章)。
_TOC_LEADER_RE = re.compile(r"[ \t]*(?:[/／]|\.{2,}|…+|·{2,})[ \t]*")

#: 目录「页码在前」形态:实测《夜行货车》整页目录印成 ``001 六月里的玫瑰花``,
#: 旧解析只认「标题 …… 页码」,这一页 8 条目录一条都读不出来,目录权威整条失效。
_TOC_LEADING_PAGE_RE = re.compile(r"^\s*(?P<page>\d{1,4})[ \t]+(?P<title>\S.*?)\s*$")

#: 章内日记的日期标题(``三月十二日`` / ``五月六日``):它比小节再低一级。
#: 后端把日期与 ``2 二哥`` 都标成 text_level=2,导航里就排成一列并列条目。
_DATE_TITLE_RE = re.compile(
    r"^[〇零一二三四五六七八九十百千\d]{1,4}月[〇零一二三四五六七八九十百千\d]{1,4}日$")

#: 带编号的小节标题(``1 长尾雉的标本`` / ``3 AMERICAN DREAM`` / ``一、历史`` /
#: ``2 ROLANTO``)。编号是书自己排的章节序列,比字号层级可靠得多。
_NUMBERED_SECTION_RE = re.compile(
    r"^(?:\d{1,3}[ \t、.．]+|[一二三四五六七八九十百千]{1,3}[、.．])\S")

#: 正文里内嵌展示的无编号全大写拉丁短行:大楼上的 ``WASHINGTON BUILDING``、
#: 报纸广告里的 ``MARKETING MANAGER``。MinerU 会按字号把它们标成 text_level=2,
#: 但它们是被引用的版面元素,不是书的小节标题。
_LATIN_DISPLAY_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .&'’\-/]*$")

#: 同页两个 heading 的垂直间距小于这个值,就按「文章标题 + 署名/栏目」的版面
#: 组合处理,而不是书的两个并列小节(实测真小节标题对的最小间距是 67)。
_INLINE_PAIR_GAP = 60.0

#: 目录条目行尾的页码
_TOC_PAGE_RE = re.compile(r"(?P<page>[0-9]{1,4})[ \t]*$")

#: 裸编号:整行只有一个数字或一个中文数字(短文里的「一」「二」「六」,不是章名)。
#: 允许带一个尾随标点 —— OCR 常把它排成「六.」「二、」,而它仍然是节内编号。
_BARE_NUMBER_RE = re.compile(r"^[0-9〇零一二三四五六七八九十百千]{1,3}[.、．,，]?$")

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


def _fold(text: str) -> str:
    """比对用的折叠:去空白 + 全角标点折成 ASCII。

    证据层文本与 md 正文常差在标点全半角/空格上(同一句在 ``content_list`` 里是
    半角逗号、在 ``full.md`` 里是全角):只去空白不够,锚点会假性失配,标题就被
    插到下一条候选所在的位置 —— 表现为「标题前还漏几段」。
    """
    return _norm(text).translate(_PUNCT_FOLD)


def _key(text: str) -> str:
    """比对键:去空白 + 全角标点折叠 + 去掉首尾装饰符号。"""
    return _fold(text).strip("-\"'.,:;<>()*_#").lower()


def _title_key(text: str) -> str:
    """标题比对键:在 :func:`_key` 基础上再剥掉脚注/公式标记。

    ``第七章 十王 $^{①}$`` 与目录里的 ``第七章 十王`` 是同一个标题;不剥标记,
    两者会被当成两个标题,层级归一和去重都认不出它。
    """
    cleaned = re.sub(r"\$\^\{[^}]*\}\$", "", text)     # $^{①}$
    cleaned = re.sub(r"\[\^[^\]\s]+\]", "", cleaned)   # [^31]
    cleaned = re.sub(r"\{#[-\w]+\}", "", cleaned)        # {#id}
    cleaned = re.sub(r"\\\*", "", cleaned)               # 脚注星号:为什么要读陈映真？\*
    return _key(cleaned)


def _sentence_like(text: str) -> bool:
    """像一句话而不像标题:整句被引号包住,或以句末标点收尾且很短。

    **编号开头的小节标题不算**:``1 凡劳苦背重担的人……`` 以省略号收尾、去空白后
    只有 11 个字,旧判据把它当「一句话」→ 记进 mid_page → 整行降回段落(实测一本
    小说集:这一节标题连同 ``#`` 与编号后的空格一起消失)。章内小节大量以
    ``1 `` / ``2 `` 起头、又爱用省略号,形态判据必须先让位给编号形态。
    """
    norm = _norm(text)
    if not norm:
        return False
    if norm[0] in "\"“‘「『" and norm[-1] in "\"”’」』":
        return True
    if norm[0].isdigit():
        return False
    return norm[-1] in _SENTENCE_END and len(norm) <= 15


def _never_a_title(text: str) -> bool:
    """三种「几乎不可能是章标题」的形态:整句引语、裸数字、不含任何文字符号。

    即使证据层自己标成 heading 也算不上:后端版面模型把强调句、对白判成标题是常见
    误判,而它们出现在导航里比「标题少一条」更刺眼。第三种是纯标点行(实测 OCR 把
    一页里的 ``=`` 标成了标题),它连一个字都没有,谈不上是章名。
    """
    norm = _norm(text)
    if not norm:
        return True
    if norm.isdigit() and len(norm) <= 3:
        return True
    if norm[0] in "\"“‘「『" and norm[-1] in "\"”’」』":
        return True
    return re.search(r"[^\W_]", norm) is None


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
    """一个待裁决的标题:名称 + 证据层序号(定位用)+ 只有目录页时的印刷页码。

    ``page_idx`` / ``y`` / ``x`` 是证据层的**版面坐标**(整本页号 + bbox 左上角),
    用来按阅读顺序裁决,而不是按后端 JSON 的块序 —— MinerU 会把页眉排到本页所有
    正文之后,按块序补回标题就会插到本章第一段后面(实测:篇名落到了小节标题之后)。
    ``subtitle`` 是页眉里被合并进来的副标题(``夜行货车华盛顿大楼之一`` →
    大标题 ``夜行货车`` + 副标题 ``华盛顿大楼之一``),补回时单独成行、不进导航。
    """

    __slots__ = ("key", "text", "ev_idx", "page", "kind", "page_idx", "y", "x",
                 "level", "subtitle")

    def __init__(self, key: str, text: str, ev_idx: int | None = None,
                 page: int | None = None, kind: str = "",
                 page_idx: int | None = None, y: float = 0.0, x: float = 0.0,
                 level: int | None = None, subtitle: str = "") -> None:
        self.key = key
        self.text = text
        self.ev_idx = ev_idx
        self.page = page
        self.kind = kind
        self.page_idx = page_idx
        self.y = y
        self.x = x
        self.level = level
        self.subtitle = subtitle


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

def _is_numbered(text: str) -> bool:
    """这条标题带书自己排的编号:裸分节号 / 编号小节 / 日记日期。

    编号是版面写明的章节序列,比后端给的 ``text_level``(其实只是字号/加粗层级)
    可靠得多 —— 判断「是不是真小节标题」时先看它。
    """
    s = text.strip()
    if not s:
        return False
    return bool(_BARE_NUMBER_RE.match(_norm(s))
                or _NUMBERED_SECTION_RE.match(s)
                or _DATE_TITLE_RE.match(_norm(s)))


def _inline_display_keys(evidence: Sequence[Block]) -> set[str]:
    """证据层标成 heading、其实只是**正文里内嵌的展示文字**的键。

    ``text_level`` 是视觉层级,不是标题语义:正文里引用的大楼招牌、报纸广告、
    文章标题与署名同样会被标成 2。两类判据都只依赖版面与形态,不猜语义:

    - 无编号、全大写拉丁短行(``WASHINGTON BUILDING`` / ``MARKETING MANAGER``);
    - 同页紧邻成对(垂直间距 < ``_INLINE_PAIR_GAP``)且都不带编号 —— 那是
      「文章标题 + 署名/栏目」的版面组合(实测 ``二哥`` / ``装配线·文秀英``
      间距 39;真小节标题对最小 67,不会被误伤)。

    带编号的小节、日期、无编号的中文小节名(``疲倦的月亮``)一律不进这个集合。
    """
    heads = [b for b in evidence
             if b.kind == "heading" and b.prov.text_level is not None
             and b.page_idx is not None and b.text.strip()]
    keys: set[str] = set()
    for block in heads:
        text = block.text.strip()
        if _is_numbered(text):
            continue
        if _LATIN_DISPLAY_RE.match(text) and re.search(r"[A-Za-z]", text):
            keys.add(_title_key(text))

    by_page: dict[int, list[Block]] = {}
    for block in heads:
        by_page.setdefault(int(block.page_idx), []).append(block)
    for group in by_page.values():
        for i, a in enumerate(group):
            if _is_numbered(a.text.strip()):
                continue
            for b in group[i + 1:]:
                if _is_numbered(b.text.strip()):
                    continue
                ba, bb = a.prov.bbox, b.prov.bbox
                if not ba or not bb:
                    continue
                if abs(float(ba[1]) - float(bb[1])) < _INLINE_PAIR_GAP:
                    keys.add(_title_key(a.text))
                    keys.add(_title_key(b.text))
    return keys


def _index_evidence(evidence: Sequence[Block],
                    toc_keys: set[str] | frozenset[str] = frozenset(),
                    ) -> tuple[dict[str, _Title], set[str], set[str]]:
    """证据层 → (标题候选, 被当作标题但其实是一句话的键)。

    两类标题块必须分开看,否则会把**每页都有的书眉**当成章标题:

    - ``heading``(后端给的字号层级)通常是**真标题**:它可能落在页面中部(实测一本
      小说的章标题在 y≈页高 27% 处),所以不按位置淘汰;只有「页中部 + 看着像一句话」
      (对白)才记进 ``mid_page`` 等降级。
    - ``header``(后端判定的页眉位置)既可能是真章标题(书的版式把章名排页首),
      也可能是**每页重复的书眉**。判据用重复:同一文本出现在 ≥3 个页面上就是书眉,
      除非目录页也列了它(那是真章名,只是被排成了书眉)。

    **不看**「这条文本在证据里是普通正文」:书名页、扉页上的书名/作者在证据层里同样
    是 ``text``,拿它当"该标题是假的"的证据会把书名标题一起降级(实测一本政经读物:
    ``# 工资、价格和利润`` 被降成段落,整本书失去唯一的一级标题)。
    """
    titles: dict[str, _Title] = {}
    mid_page: set[str] = set()
    heights = _page_heights(evidence)
    pages_of: dict[str, set[int]] = {}
    for block in evidence:
        if block.kind in _TITLE_KINDS and block.page_idx is not None:
            pages_of.setdefault(_title_key(block.text), set()).add(block.page_idx)
    for idx, block in enumerate(evidence):
        key = _title_key(block.text)
        if not key:
            continue
        if block.kind not in _TITLE_KINDS:
            continue
        text = block.text.strip()
        if block.kind == "header":
            repeated = len(pages_of.get(key, ())) >= _RUNNING_HEAD_PAGES
            if repeated and key not in toc_keys:
                continue                        # 每页重复 = 书眉
        elif _sentence_like(text) and _page_top(block, heights) is False:
            mid_page.add(key)                   # 页中部的一句话(对白),不是标题
            continue
        if key in titles or not _title_like(block):
            continue
        box = block.prov.bbox
        titles[key] = _Title(
            key, _clean_title(text), idx, block.page_idx, kind=block.kind,
            page_idx=block.page_idx,
            y=float(box[1]) if box else 1e9,
            x=float(box[0]) if box else 1e9,
            level=block.prov.text_level if block.prov.text_level is not None else block.level,
        )
    running = {k for k, pages in pages_of.items() if len(pages) >= _RUNNING_HEAD_PAGES}
    return titles, mid_page, running


def _title_like(block: Block) -> bool:
    """这条证据像不像**标题**(而不是页眉/页码/整段正文)。"""
    text = block.text.strip()
    key = _norm(text)
    if not key or len(key) > _TITLE_MAX:
        return False
    # 裸数字(``2`` / ``3`` / ``4``)有两种身份:没有层级标注时是残留页码;后端
    # 明确给了 ``text_level`` 时是**章内分节编号**,是货真价实的小标题。实测一本
    # 小说集里 3 个分节号被这里剔除、随后又在降级/页码规则里被删得一字不剩。
    if is_page_number_line(text) and block.prov.text_level is None:
        return False
    if _is_catalog_entry(text):
        return False
    if _TOC_TAIL_RE.search(text):           # 「译序 / i」这类条目形态(罗马数字页码)
        return False
    if _is_block_start(text):
        return False                       # md 块标记出现在证据里 = 不是标题文本
    return True


def _parse_toc_line(line: str) -> tuple[str, str, str] | None:
    """目录条目行 → ``(标题, 作者, 页码)``;不是条目时返回 ``None``。

    三种形态都要认(都是排版写明的列,不是猜的):

    - ``标题 …… 35`` / ``标题 / 35``;
    - ``标题 …… 作者 35``:目录带作者列,实测扫描书每行都是「沉船礁……齐平 3」;
    - ``标题 作者  35``(裸空格分隔,弱形式):由 ``_is_catalog_entry`` 把关;
    - ``001 六月里的玫瑰花``:**页码在前**(实测《夜行货车》整页目录),``379 为什么
      要读陈映真？/赵刚`` 还会带作者列。页码在前的行不以数字结尾,
      ``_is_catalog_entry`` 认不出,旧逻辑整页 8 条目录一条都没读到。

    标题取**第一个引线之前**的文字,作者取引线与页码之间的文字 —— 少了这一步,
    带作者列的目录整页都读不出来,「目录是权威」也就无从谈起。
    """
    s = line.strip()
    if not s:
        return None
    leading = _TOC_LEADING_PAGE_RE.match(s)
    if leading:
        head, page = leading.group("title").strip(), leading.group("page")
        if not head or len(_norm(head)) > _TITLE_MAX:
            return None
        lead = _TOC_LEADER_RE.search(head)
        if lead is None:
            title, author = head, ""
        else:
            title = head[: lead.start()].strip()
            author = head[lead.end():].strip().strip("()（）[]【】")
        if not title:
            return None
        return title, author, page
    if not _is_catalog_entry(s):
        return None
    m = _TOC_PAGE_RE.search(s)
    if not m:
        return None
    head = s[: m.start()].strip()
    if not head:
        return None
    lead = _TOC_LEADER_RE.search(head)
    if lead is None:
        # 弱形式(只有裸空格):标题与作者列分不开,只认标题 —— 硬切会把
        # 「第一章 绪论 35」切成标题「第一章」、作者「绪论」
        return head, "", m.group("page")
    title = head[: lead.start()].strip()
    author = head[lead.end():].strip().strip("()（）[]【】")
    if not title:
        return None
    return title, author, m.group("page")


def _author_keys(author: str) -> set[str]:
    """作者栏 → 比对键集合(多个名字用空格/顿号分开时逐个记)。"""
    keys = {_key(part) for part in re.split(r"[\s、,，/]+", author or "")}
    keys.add(_key(author or ""))
    keys.discard("")
    return keys


def _same_name(a: str, b: str) -> bool:
    """两个**人名**是否同名;三字以上允许一个字不同(OCR 形近字)。"""
    if a == b:
        return True
    if len(a) < 3 or len(a) != len(b):
        return False
    return sum(1 for x, y in zip(a, b, strict=True) if x != y) <= 1


def _byline_like(text: str, authors: set[str]) -> bool:
    """这个「标题」其实是目录作者栏里的那个人名。

    目录带作者列时,后端常把署名行也标成 heading(实测一本短篇集:每篇的作者名都
    成了一个章)。判据只有一条:它就是目录作者栏里的名字 —— 不靠「短、没标点」
    这类形态猜,免得把真正的短篇名(「在大路上」)一起删掉。
    """
    key = _key(text)
    if not key or not authors:
        return False
    return any(_same_name(key, author) for author in authors)


def _toc_titles(lines: Sequence[str]) -> tuple[dict[str, _Title], set[str]]:
    """目录页 → ``(标题候选, 作者键集合)``。

    只认**连续 ≥3 行**且每行都是「标题 + 页码」的块:单行 ``…/35`` 可能是正文里的
    引用,连续三行以上才是目录。作者键只从这些确认过的条目里取 —— 没成块的「标题
    作者 页码」不当目录,自然也不该拿它的中间那截去删署名。
    """
    found: dict[str, _Title] = {}
    authors: set[str] = set()
    run: list[tuple[str, str, str, str]] = []       # (标题, 作者, 页码, 原行)

    def flush() -> None:
        # 「页码在前」的目录(``001 六月里的玫瑰花``)不与正文里 ``1 小节名`` 同形:
        # 要求整段页码**位数一致、严格递增**,否则宁可不认这一段目录 —— 误把正文的
        # 连续编号当目录,会让目录权威反过来把真章名降成小节。
        if all(_TOC_LEADING_PAGE_RE.match(raw.strip()) for _t, _a, _p, raw in run):
            pages = [int(p) for _t, _a, p, _raw in run if p.isdigit()]
            widths = [len(p) for _t, _a, p, _raw in run]
            if (len(pages) != len(run) or len(set(widths)) != 1 or min(widths) < 2
                    or any(a >= b for a, b in zip(pages, pages[1:]))):
                run.clear()
                return
        for title, author, page, _raw in run:
            if author:
                authors.update(_author_keys(author))
            key = _title_key(title)
            if not _norm(title) or key in found:
                continue
            page_no = int(page) if page.isdigit() else None
            found[key] = _Title(key, _clean_title(title), None, page_no)
        run.clear()

    for line in lines:
        s = line.strip()
        if not s:
            continue
        parsed = _parse_toc_line(s)
        if parsed is not None:
            run.append((*parsed, s))
        else:
            if len(run) >= _TOC_MIN_RUN:
                flush()
            else:
                run.clear()
    if len(run) >= _TOC_MIN_RUN:
        flush()

    return found, authors


def _adopt_gap_headings(lines: list[str], toc: dict[str, _Title],
                        toc_keys: set[str], struct_keys: set[str],
                        chapter_level_keys: set[str] | None = None) -> dict[str, str]:
    """按**目录顺序**补认「字形变了但位置对得上」的章标题,返回补认到的目录键。

    为什么需要:OCR 会把同一本书排成繁简/异体两套字(目录印「巡线工」「骏马飞驰」
    正文却是「巡 綫 工」「駿馬飞馳」),按文字比对永远配不上 —— 于是真章标题被降成
    小节,挂在上一篇底下(实测一本短篇集:两篇丢章,导航层级也跟着错)。

    判据只有位置:目录顺序 = 阅读顺序。目录里前后两条都找到了落点,中间**恰好**只
    剩一个标题时,它就是这条目录对应的章;剩 0 个或 2 个以上都不猜。

    ``chapter_level_keys`` 是证据层明确标了 ``text_level=1`` 的标题(署名已由目录
    作者栏排除)。真实短篇集里每章夹着 3–6 个 ``text_level=2`` 小节,「窗口里恰好
    一个标题」几乎不成立;但**章级信号在窗口里是唯一的**,先把候选收敛到这些强信号
    再数。强信号 2 个以上仍不猜;整本书没有 L1 信息时退回原判据,不影响旧夹具。
    """
    order = list(toc)
    pos_of: dict[str, int] = {}
    for pos, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        key = _title_key(m.group(2))
        if key in toc_keys:
            pos_of.setdefault(key, pos)
    adopted: dict[str, str] = {}          # 目录键 → 正文里那个字形不同的标题键
    for i, key in enumerate(order):
        if key in pos_of:
            continue
        lo = next((pos_of[order[j]] for j in range(i - 1, -1, -1)
                   if order[j] in pos_of), -1)
        hi = next((pos_of[order[j]] for j in range(i + 1, len(order))
                   if order[j] in pos_of), len(lines))
        cands: list[int] = []
        for pos in range(lo + 1, hi):
            m = _HEADING_RE.match(lines[pos])
            if not m:
                continue
            other = _title_key(m.group(2))
            if other in toc_keys or other in struct_keys:
                continue
            cands.append(pos)
        if chapter_level_keys:
            # 章内小节(text_level=2)会挤爆窗口,但章级信号只有一个:先只数 L1。
            strong = [p for p in cands
                      if _title_key(_HEADING_RE.match(lines[p]).group(2))
                      in chapter_level_keys]
            if len(strong) > 1:
                continue                    # 两个以上章级候选,仍然不猜
            if len(strong) == 1:
                cands = strong
        if len(cands) != 1:
            continue
        text = _HEADING_RE.match(lines[cands[0]]).group(2).strip()
        lines[cands[0]] = f"# {text}"        # 章级:小节与章在同一个 xhtml 里分不开
        pos_of[key] = cands[0]
        adopted[key] = _title_key(text)
    return adopted

def _dedupe_chapter_numbers(titles: dict[str, _Title], toc_keys: set[str]) -> None:
    """同一章号只留一个标题(OCR 会把书眉认成不同的字,如「第一章 托神」/「托襟」)。

    真章名在目录里;目录点名的那个优先,目录一条都没命中时才留第一个。没有章号前缀
    的标题(短篇名)不受影响 —— 同名短篇集里的「烧纸」是章节名,不是重复。
    """
    groups: dict[str, list[str]] = {}
    for key, entry in titles.items():
        found = _CHAPTER_NO_RE.match(_fold(entry.text))
        if found:
            groups.setdefault(found.group(0), []).append(key)
    for keys in groups.values():
        if len(keys) < 2:
            continue
        kept = [k for k in keys if k in toc_keys] or keys[:1]
        for key in keys:
            if key not in kept:
                titles.pop(key, None)


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
        if block.kind in _ANCHOR_SKIP_KINDS or block.id == title.id:
            return None                       # 标题自己不能当锚点(会匹配到标题行本身)
        key = _fold(block.text)
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

    比较用 :func:`_fold`(忽略空白与全半角标点差异),并**剥掉行首的 md 标题标记** ——
    否则 ``## 死后所留下的`` 配不上锚点 ``死后所留下的``,标题就会跳到本章第 3、4 段
    之后(实测:楔子、第八章都因此插晚)。

    ``exact=True`` 要求整行与锚点相等(锚点本身就是整块的完整文字):短句常常被别处
    的长段落引用,按"包含"匹配会落到引用它的那段文字里。
    """
    for pos in range(max(0, start), len(lines)):
        text = lines[pos].strip()
        while text.startswith("#"):
            text = text.lstrip("#").strip()
        folded = _fold(text)
        if (folded == anchor) if exact else (anchor in folded):
            return pos
    return None


def _heading_near(lines: Sequence[str], pos: int, key: str) -> bool:
    """``pos`` 正上方(跳过空行与页码注释)**紧邻**的是不是同名标题。

    只认紧邻:同名标题出现在更远的位置时,它八成是书名页上的书名或被正文引用的同一句,
    不是本章的章标题 —— 那种情况下要补一个新的,旧的随后会被降级。
    """
    for j in range(pos - 1, -1, -1):
        s = lines[j].strip()
        if not s or _is_page_mark(s):
            continue
        m = _HEADING_RE.match(s)
        return bool(m) and _title_key(m.group(2)) == key
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
        if _title_key(s) != key:
            continue
        before = lines[pos - 1].strip() if pos > 0 else ""
        after = lines[pos + 1].strip() if pos + 1 < len(lines) else ""
        if before:
            continue
        if not after or (len(_norm(after)) <= 20 and after[-1] not in "。！？.!?；;"):
            return pos
    return None


def _split_header_subtitle(header_text: str, title: str) -> str:
    """页眉把「大标题 + 副标题」合成一行时,切出副标题(不进导航)。

    ``夜行货车华盛顿大楼之一`` → ``华盛顿大楼之一``;
    ``万商帝君\\n华盛顿大楼之四`` → ``华盛顿大楼之四``。
    去空白后按前缀切,没有前缀关系时退回「第一行之后的所有行」。
    """
    flat = re.sub(r"\s+", "", header_text)
    head = re.sub(r"\s+", "", title)
    if head and flat.startswith(head):
        return flat[len(head):].strip()
    parts = [p.strip() for p in header_text.splitlines() if p.strip()]
    return " ".join(parts[1:]).strip() if len(parts) > 1 else ""


def _toc_page_offset(toc: dict[str, _Title], evidence: Sequence[Block]) -> int | None:
    """目录印刷页码 → PDF 页号的固定偏移(卷首页数),只有可验证时才返回。

    只看能**双向对上**的条目:目录标题在证据层里有同名块(或页眉把标题与副标题
    连写、以目录标题开头),两者页号之差即候选偏移;至少 2 条一致才采用。实测
    《夜行货车》:001→6、027→32、073→78、121→126、262→267、397→402,偏移恒为 5。

    有了它,正文里连一行都没识别出来的大标题也能按页补回 —— 目录印「153 云」,
    OCR 把那个大字整个漏了,只能靠印刷页码落到 PDF 第 158 页的章首。
    """
    from collections import Counter

    ev_pages: dict[str, list[int]] = {}
    heights = _page_heights(evidence)
    for block in evidence:
        # 只认**页首的标题类块**:页脚每页都印着篇名(实测「夜行货车」出现 121 次),
        # 把它们算进来会得到一堆 6..18 的假偏移,众数被拖成 15 —— 目录页码于是
        # 整本错 10 页,章标题全插进上一篇的正文中。页中部的 CIP 页字段
        # (``## 夜行货车`` 印在版权页中部)同理不是章首。
        if (block.page_idx is None or block.kind not in _TITLE_KINDS
                or _page_top(block, heights) is False):
            continue
        key = _title_key(block.text)
        if key:
            ev_pages.setdefault(key, []).append(block.page_idx)
    offsets: list[int] = []
    for key, entry in toc.items():
        if entry.page is None:
            continue
        pages = set(ev_pages.get(key, ()))
        if not pages:
            for other, values in ev_pages.items():
                if other.startswith(key) and 0 < len(other) - len(key) <= 24:
                    pages.update(values)
        offsets.extend(page - entry.page for page in pages)
    if len(offsets) < 2:
        return None
    off, count = Counter(offsets).most_common(1)[0]
    return off if count >= 2 else None


def _first_line_of_page(lines: Sequence[str], evidence: Sequence[Block],
                        page: int, cursor: int) -> int | None:
    """该页版面最靠上的内容块 → md 里的行号(把章标题插在它之前)。

    用于「目录给了印刷页码、正文里却一行标题都没有」的章。只认版面坐标,不做
    语义猜测:同页按 ``(y, x)`` 取前几个内容块,短块要求整行相等,逐个回 md 找。
    """
    candidates = [b for b in evidence
                  if b.page_idx == page and b.kind not in _ANCHOR_SKIP_KINDS and b.text.strip()]
    for block in sorted(candidates, key=_reading_pos)[:6]:
        key = _fold(block.text)
        if not key:
            continue
        pos = _find_line(lines, key[:_ANCHOR_CHARS], cursor,
                         exact=len(key) <= _ANCHOR_CHARS)
        if pos is not None:
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
    toc, toc_authors = _toc_titles(lines)
    toc_keys = set(toc)
    toc_authoritative = len(toc_keys) >= _MIN_TITLE_SOURCES
    ev_titles, mid_page, running_heads = _index_evidence(evidence, toc_keys)
    inline_keys = _inline_display_keys(evidence)
    hint_key = _key(title_hint) if title_hint else ""

    # 内嵌展示文字不进标题候选:否则它们会以「证据层背书」的身份逃过降级
    titles: dict[str, _Title] = {k: v for k, v in ev_titles.items()
                                 if k not in inline_keys}
    for key, entry in toc.items():
        # 页眉常把「大标题 + 副标题」连写成一行(``夜行货车华盛顿大楼之一``),
        # 目录只印大标题。**先找页首 header**:版权页中部也常印一个同名书名
        # (``## 夜行货车``),按 key 相等先合并会把章标题的落点定到版权页上,
        # 真正的章首反而一条标题都没有。
        match = next((k for k, v in titles.items()
                      if v.kind == "header"
                      and (k == key or (_title_key(v.text).startswith(key)
                                        and 0 < len(_title_key(v.text)) - len(key) <= 24))), None)
        if match:
            ev = titles.pop(match)
            ev.subtitle = _split_header_subtitle(ev.text, entry.text) if match != key else ""
            ev.key = key
            ev.text = entry.text
            ev.page = entry.page
            titles[key] = ev
        elif key in titles:
            # 目录页给的是印刷标题,优先用它(证据层那一行可能是被拆开的)
            titles[key].text = entry.text
            titles[key].page = entry.page
        else:
            titles[key] = entry
    _drop_running_heads(titles, evidence, keep=toc_keys)
    if len(toc_keys) >= _MIN_TITLE_SOURCES:
        # 目录是权威:目录列出来的才是「章」。证据层里其余的 ``heading`` 是章内小节
        # (实测小说每章下面还有 2–3 个栏目名),保持自己的层级,不提升成章。
        # 但 ``header`` 类的单次标题要留下:那是「书的版式把章/节名排成页眉」——
        # 目录不列它(如译序,或 ``3 AMERICAN DREAM`` 这种章内编号小节),丢了就少一节。
        titles = {k: v for k, v in titles.items()
                  if k in toc_keys
                  or (v.kind == "header"
                      and (_FRONT_BACK_SECTION_RE.match(_fold(v.text))
                           or _NUMBERED_SECTION_RE.match(v.text.strip())))}
    # 页首编号页眉小节:不在目录里,但是章内小节(二级),不能跟章一起被统一成一级
    section_keys = {k for k, v in titles.items()
                    if k not in toc_keys and v.kind == "header"
                    and _NUMBERED_SECTION_RE.match(v.text.strip())}
    _dedupe_chapter_numbers(titles, toc_keys)
    page_offset = _toc_page_offset(toc, evidence) if toc_authoritative else None

    existing = {_title_key(m.group(2)) for m in map(_HEADING_RE.match, lines) if m}
    struct_keys = {_key(label) for label in _STRUCT_LABELS}
    inserted = promoted = 0
    missing: list[str] = []
    cursor = 0

    def layout_order(t: _Title) -> tuple:
        """按版面阅读顺序排,不按后端 JSON 块序。

        MinerU 把 ``header`` 排在本页所有正文之后;按块序补回时,篇名会插到本章
        第一个小节标题**之后**(实测:``# 疲倦的月亮`` 排在 ``# 六月里的玫瑰花``
        前面、``# 1 长尾雉的标本`` 排在篇名前面)。目录给了印刷页码的,换算成
        整本页号后排在该页最前。
        """
        if t.page_idx is not None:
            return (0, t.page_idx, t.y, t.x)
        if t.page is not None and page_offset is not None:
            return (0, t.page + page_offset, -1.0, 0.0)
        return (1, t.ev_idx if t.ev_idx is not None else 10 ** 9, 0.0, 0.0)

    for entry in sorted(titles.values(), key=layout_order):
        if entry.key in struct_keys:
            continue
        if (entry.page or 0) == 0:
            continue                        # 第 1 页 = 卷首(书名页),由 front_matter 处理
        pos = None
        if entry.ev_idx is not None:
            for anchor, exact in _anchors_for(evidence[entry.ev_idx], evidence):
                pos = _find_line(lines, anchor, cursor, exact=exact)
                if pos is not None:
                    break
        if pos is not None and entry.key in existing:
            if _heading_near(lines, pos, entry.key):
                cursor = pos                  # 章首已经有这个标题,不重复插
                continue
            # 同名标题不在章首 = 别处的同名文字(书名页/正文里的引用),先降级再补
            for j, line in enumerate(lines):
                m = _HEADING_RE.match(line)
                if m and _title_key(m.group(2)) == entry.key:
                    lines[j] = m.group(2).strip()
            existing.discard(entry.key)
        if pos is None:
            if entry.key in existing:
                # 正文里已经是同名标题(证据层没给落点、但后端标的标题是对的):
                # 层级与位置由下面的去重/层级归一负责,这里不能报「找不到落点」
                continue
            # 1 个字的标题不接受「从正文段落里就地提升」(没有判别力),但目录点名 +
            # 页码映射是显式来源,不该被这门槛拦住 —— 实测大标题「云」只有一个字,
            # OCR 整个漏掉,旧逻辑在提升前就把它记成「找不到落点」。
            hit = (_standalone_line(lines, entry.key)
                   if len(_norm(entry.text)) >= _PROMOTE_MIN_CHARS else None)
            if hit is not None:
                # 尾部 ``\*`` 是正文里的脚注标记(``为什么要读陈映真？\*``),提升成
                # 标题时留在标题里会渲染成一颗孤立星号,顺手剥掉;正文不变。
                label = re.sub(r"\\\*\s*$", "", lines[hit].strip()).strip()
                lines[hit] = f"# {label}"
                existing.add(entry.key)
                promoted += 1
                cursor = max(cursor, hit)
                continue
            # 正文里连一行都没有、目录却点名的章(实测大标题「云」被 OCR 整个漏掉):
            # 就地提升失败后,才用「印刷页码 + 可验证的卷首偏移」换算成整本页号,
            # 插在该页章首之前。有独立行的标题一律就地提升 —— 页映射只是兜底,
            # 抢在前面会因为同一段文字在别处重复而插进上一篇的中段。
            page_pos = (_first_line_of_page(lines, evidence,
                                            entry.page + page_offset, cursor)
                        if page_offset is not None and entry.page is not None else None)
            if page_pos is None:
                missing.append(entry.text)
                continue
            pos = page_pos
        if pos > 0 and lines[pos - 1].strip():
            lines.insert(pos, "")
            pos += 1
        prefix = "##" if entry.key in section_keys else "#"
        block_lines = [f"{prefix} {entry.text}"]
        if entry.subtitle:
            block_lines += ["", entry.subtitle]
        block_lines.append("")
        lines[pos:pos] = block_lines
        existing.add(entry.key)
        inserted += 1
        cursor = pos + len(block_lines)

    # 同名标题只留**章首那一个**:md 开头常有 ``# 书名``(扉页/版权页),同名短篇集
    # 又会让章标题与它同名 —— pandoc 的 section id 相同,nav 里两条目录项会指向同一处
    # (实测:第二个「烧纸」跳回书名页)。章在书里出现得比书名页晚,所以留最后一个。
    for key in titles:
        spots = [pos for pos, line in enumerate(lines)
                 if (m := _HEADING_RE.match(line)) and _title_key(m.group(2)) == key]
        for pos in spots[:-1]:
            lines[pos] = _HEADING_RE.match(lines[pos]).group(2).strip()

    # 章标题统一到最外层:目录/证据点名的标题若还带着 ``##``(后端标的层级),pandoc
    # 会把它挂到上一章下面 —— 导航里就成了子项,按章切分也跟着错。
    levels = [len(m.group(1)) for m in map(_HEADING_RE.match, lines)
              if m and _title_key(m.group(2)) in titles
              and _title_key(m.group(2)) not in section_keys]
    base = min(levels) if levels else 0
    if base:
        for pos, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m or _title_key(m.group(2)) not in titles:
                continue
            if _title_key(m.group(2)) in section_keys:
                continue                       # 页首编号页眉小节保持二级
            if len(m.group(1)) != base:
                lines[pos] = "#" * base + " " + m.group(2)

    demoted: list[str] = []
    if len(titles) >= _MIN_TITLE_SOURCES:
        # 后端明确给了 ``text_level`` 的 heading:裸编号(``2``/``3``/``4``)是分节号,
        # ``1 凡劳苦背重担的人……`` 是带编号的小节名 —— 两者都不是残留页码,也不是
        # 一句对白。目录权威也不能把它们降成正文:实测一本小说集里 3 个分节号被降级
        # 后又被 ``page_numbers`` 规则整行删掉,正文里连编号都没了。
        explicit_level_keys = {
            _title_key(b.text) for b in evidence
            if b.kind == "heading" and b.prov.text_level is not None
        }
        first_heading = next((pos for pos, line in enumerate(lines)
                              if _HEADING_RE.match(line)), None)
        first_chapter = next((pos for pos, line in enumerate(lines)
                              if (m := _HEADING_RE.match(line))
                              and _title_key(m.group(2)) in titles), None)
        for pos, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m:
                continue
            text = m.group(2).strip()
            key = _title_key(text)
            if key in inline_keys:
                # 证据层按字号标成标题,其实是正文里引用的招牌/广告/文章标题+署名
                lines[pos] = text
                demoted.append(text[:24])
                continue
            if key in toc_keys:
                continue                       # 目录点名 = 真章名,不降
            if key in explicit_level_keys and (
                    _BARE_NUMBER_RE.match(_norm(text))
                    or (_norm(text) and _norm(text)[0].isdigit())):
                continue                       # 证据层标了层级的编号小节,保留为标题
            if toc_authoritative and (
                    key in struct_keys
                    or _byline_like(text, toc_authors)
                    or _BARE_NUMBER_RE.match(_norm(text)) is not None
                    or text.rstrip().endswith(("：", ":"))):
                # 目录是权威时,这四类"标题"都不是章:
                #   目录页自己的标签(目次/目录)、目录作者栏里的人名(署名行)、
                #   裸编号(短篇内部的一/二/六)、以冒号收尾的称呼或字段行。
                # 实测一本短篇集:「目次」「齐平」「六」「八」「=」「負責同志：」
                # 全都进了导航,与目次 21 条对不上。判据都来自目录或形态本身,
                # 不做"短、没标点就删"的猜测。
                lines[pos] = text
                demoted.append(text[:24])
                continue
            if (key in running_heads and key not in toc_keys
                    and pos != first_heading):
                # 书眉上的文字被 md 写成了标题(实测:每页都印书名,后端把某一页的
                # 书名标成了 heading)。它每页都有,不是章节标题。
                lines[pos] = text
                demoted.append(text[:24])
                continue
            if key in titles and not _never_a_title(text):
                continue                       # 证据层背书
            if _impossible_title(text) or (key in mid_page and _sentence_like(text)):
                lines[pos] = text
                demoted.append(text[:24])
                continue
            if (len(toc_keys) >= _MIN_TITLE_SOURCES and first_chapter is not None
                    and pos < first_chapter and text not in _STRUCT_LABELS):
                # 卷首(第一个章标题之前)的杂标题:版权声明/刊印说明之类
                lines[pos] = text
                demoted.append(text[:24])

    adopted: dict[str, str] = {}
    if toc_authoritative:
        # 字形变了但位置对得上的章标题(繁简/异体),按目录顺序补认;必须在**降级之后**
        # 跑:只有真标题(署名/裸编号已被降成正文)才剩下,窗口里才可能「恰好一个」。
        # 章级强信号:后端把章标题标成 text_level=1、章内小节标成 2。署名也常被
        # 标成 1(实测一本短篇集 26 个 L1 里有 6 个是作者名),用目录作者栏排掉。
        chapter_level_keys = {
            _title_key(b.text) for b in evidence
            if (b.kind == "heading" and b.prov.text_level == 1 and b.text.strip()
                and not _byline_like(b.text, toc_authors)
                and _title_key(b.text) not in toc_authors)
        }
        adopted = _adopt_gap_headings(lines, toc, toc_keys, struct_keys,
                                      chapter_level_keys)
        for body_key in adopted.values():
            text = next((ln.lstrip("#").strip() for ln in lines
                         if (m := _HEADING_RE.match(ln))
                         and _title_key(m.group(2)) == body_key), "")
            titles[body_key] = _Title(body_key, text)   # 认作章:不再被降成小节
        if adopted:
            missing = [t for t in missing if _title_key(t) not in adopted]

    if toc_authoritative and base:
        # 目录之外的真标题是**章内小节**:统一降到章下面一级。它们仍是标题(小节名
        # 是内容),但不再与章并列 —— 后端把每个小标题都标成一级时,导航里会排出一整
        # 列与章同级的条目(实测:目次 21 条的书里出现 41 条)。
        for pos, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if not m or _title_key(m.group(2)) in titles:
                continue
            text = m.group(2).strip()
            if _DATE_TITLE_RE.match(_norm(text)):
                # 日记体里的日期比小节再低一级:``2 二哥`` 是小节,``三月十二日``
                # 是它下面的日期。后端把两者都标成 text_level=2,旧逻辑一律降到
                # 章下一级,导航里就排成一长列并列条目。
                lines[pos] = "#" * min(6, base + 2) + " " + text
            elif len(m.group(1)) <= base:
                lines[pos] = "#" * min(6, base + 1) + " " + m.group(2)

    if section_keys and base:
        # 页首编号页眉小节统一到「章下一级」:它不在目录里,不能与章并列
        for pos, line in enumerate(lines):
            m = _HEADING_RE.match(line)
            if m and _title_key(m.group(2)) in section_keys:
                lines[pos] = "#" * min(6, base + 1) + " " + m.group(2)

    notes: list[str] = []
    if inserted or promoted or demoted or adopted:
        notes.append(
            f"标题裁决:{len(titles)} 个标题来源,补回 {inserted} 个、提升 {promoted} 个、"
            f"按目录顺序补认 {len(adopted)} 个、"
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
