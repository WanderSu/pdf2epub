"""清理规则(P6:IR/Patch 体系下的**唯一**实现)。

分工边界:这里**只提出候选**(``Patch.propose``),能不能改由 ``clean.engine`` 依据
``confidence`` / ``evidence`` / ``policy`` 判定。语义判据一律从
``markdown.textrules`` 导入(END_PUNCT、JOIN_MIN_CHARS、_is_catalog_entry…),
不复制一份 —— 两份判据迟早各自漂移。

十条规则里哪些改正文、哪些只报结论:

======================  ==================================================
page_numbers            整行是页码 → 删除该行(整块只有页码 → 删块)
running_heads           全文重复 ≥3 次的短行 → 删除该行
front_matter            第 1 页只是书名/作者/出版社 → 整块删除(封面已另作 cover.jpg)
cjk_spaces              汉字/中文标点/数字之间的空格 → 去掉
blank_lines             块间 ≥3 连续换行 → 2(文首空白走文档级 set_prefix)
trailing_spaces         行尾空白 → 去掉(代码/公式/脚注/注释块整体跳过)
join_lines              被断开的段落拼接 + 诗行补硬换行
ocr_spaces              OCR 拆开的拉丁词合并(``Py Mu PDF`` → ``PyMuPDF``)
dup_headings            相邻同级同名标题去重
headings                空标题删除 + 整书层级基准归一 + 层级跳跃收敛
images                  **只校验**:引用缺失写进报告 issues,不改正文
======================  ==================================================

跨块拼接 = ``replace_text(A) + delete_block(B)`` 两条 Patch,delete 带 ``depends_on``:
前置替换没 applied,块一个都不删。A 的 ``after.trailer`` 记 B 原来的块间空行。
删除是破坏性操作,必须有结构证据:被吸收块**有证据层链接**(结构块背书)才提候选;
没有链接的跨块拼接降级为 ``suggested`` 的删除建议(只记不动),保证「正文并了、
块还在」这种**重复内容**永远不可能被提出。

证据不足时宁可跳过 —— 三条文本规则(join_lines / cjk_spaces / ocr_spaces)的自信
程度是 ``heuristic``;正式流程是否允许它们落地由 ``clean.policy`` 显式表态,不在这里
偷偷放宽。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace as _dc_replace

from clean.options import CleanOptions
from clean.patch import AfterState, Evidence, Patch, Validation
from clean.policy import NON_PATCH_RULES, PIPELINE_ORDER, enabled_rules
from ir.model import Block, BookIR
from markdown.textrules import (ADJACENT_MIN_CHARS, CROSS_BLANK_MIN_CHARS,
                                CJK_CHARS, END_PUNCT, HARD_BREAK, INLINE_CODE_RE,
                                START_PUNCT, VERSE_MAX_CHARS, _is_block_start,
                                _is_catalog_entry, _is_layout_unit, _is_note_line,
                                _is_page_mark, _is_running_head_candidate,
                                _is_structure_line, _is_verse_line,
                                _keep_line_break, _looks_like_layout_line,
                                is_page_number_line)

#: OCR 拆词合并的参数(缺省值),显式写出以便与判据同源审计
OCR_MAX_FRAG = 4
OCR_MIN_FRAGS = 3
OCR_MIN_SHORT = 2

#: legacy 里被整体保护的结构(代码/脚注/数学)→ 原子单元:任何文本规则都不碰
_ATOMIC_KINDS = frozenset({"code", "table", "equation", "footnote"})
_ATOMIC_ROLES = frozenset({"html_comment", "horizontal_rule"})

#: 「行尾空白」这一条要排除的块:代码/公式/脚注(行尾空白是内容)、注释与分隔线
#: (与空行压缩同一取舍,见 ``propose_blank_lines``)。表格**不排除** —— legacy
#: 也没把表格当掩码块,行尾空白照样清掉。
_TRAILING_SKIP_KINDS = frozenset({"code", "equation", "footnote"})
_TRAILING_SKIP_ROLES = frozenset({"html_comment", "horizontal_rule", "page_marker"})


def _is_atomic(block: Block) -> bool:
    role = str(block.extra.get("role") or "")
    return block.kind in _ATOMIC_KINDS or role in _ATOMIC_ROLES


def _is_page_marker(block: Block) -> bool:
    return block.kind == "unknown" \
        and str(block.extra.get("role") or "") == "page_marker"


def _blanks(ws: str) -> int:
    """一段块间空白里有几个空行(``"\n"`` 只是行尾,``"\n\n"`` 才是 1 个空行)。"""
    return max(0, ws.count("\n") - 1)


def _linked_evidence(ir: BookIR, block: Block) -> Evidence | None:
    """块的结构证据:对齐阶段挂上的 evidence 链接。没有就 None。"""
    for link in ir.links:
        if link.block_id != block.id:
            continue
        ev = ir.block(link.evidence_id)
        if ev is not None and ev.text.strip():
            return Evidence(kind="linked_content", page_idx=ev.page_idx,
                            matched=ev.text.strip()[:64], source="evidence_block")
    return None


def _whole_block_evidence(block: Block, kind: str = "structural") -> Evidence:
    """「这一整块就是我要删的东西」——自身证据。

    与 ``text_rule`` 的区别在 ``scope``:``whole`` 表示 ``matched`` 覆盖整块(不是
    在某一行里命中一个片段)。引擎只对"整块命中 + 判定是推导而非猜测"放行删除,
    见 ``clean.engine._structural_self_evidence``。
    """
    return Evidence(kind=kind, matched=block.text.strip(), source="md_block",
                    scope="whole")


def _line_evidence(matched: str, kind: str = "structural") -> Evidence:
    """行级命中的证据(``matched`` 是被删掉的那几行原文)。"""
    return Evidence(kind=kind, matched=matched.strip()[:64], source="md_block")


@dataclass(frozen=True)
class RuleProposal:
    """一条规则对一份 IR 的提案:候选 Patch + 给人看的说明。"""

    rule_id: str
    patches: tuple[Patch, ...] = ()
    notes: tuple[str, ...] = ()


# ================================================================ 行级过滤骨架

def _filter_lines(ir: BookIR, *, rule_id: str, first_id: int,
                  drop_line, atomic_skip: frozenset[str] = _ATOMIC_KINDS) -> RuleProposal:
    """按行过滤的共用骨架:命中即删行,整块命中即删块。

    返回的 Patch 顺序 = 块顺序(执行顺序即给定顺序,不重排)。
    """
    patches: list[Patch] = []
    notes: list[str] = []
    id_counter = first_id
    dropped_lines = 0
    dropped_blocks = 0

    for blk in sorted(ir.editable_blocks, key=lambda b: b.order):
        if blk.kind in atomic_skip or _is_page_marker(blk):
            continue
        lines = blk.text.split("\n")
        kept = [line for line in lines if not drop_line(line)]
        if len(kept) == len(lines):
            continue
        dropped_lines += len(lines) - len(kept)
        if not kept:
            patches.append(Patch.propose(
                patch_id=f"p{id_counter:04d}", rule_id=rule_id, block=blk,
                operation="delete_block", evidence=_whole_block_evidence(blk),
                confidence="derived", risk="low",
            ))
            dropped_blocks += 1
        else:
            matched = "\n".join(line for line in lines if drop_line(line))
            patches.append(Patch.propose(
                patch_id=f"p{id_counter:04d}", rule_id=rule_id, block=blk,
                operation="replace_text", after_text="\n".join(kept),
                evidence=_line_evidence(matched),
                confidence="derived", risk="low",
            ))
        id_counter += 1

    if dropped_lines:
        notes.append(f"删除页码行 {dropped_lines} 行"
                     f"(其中整块删除 {dropped_blocks} 块)"
                     if rule_id == "page_numbers" else
                     f"删除重复行 {dropped_lines} 行(其中整块删除 {dropped_blocks} 块)")
    return RuleProposal(rule_id, tuple(patches), tuple(notes))


# ================================================================ page_numbers

def propose_page_numbers(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """剔除残留页码:独立纯数字行(1-3 位)与中文形态(``第 3 页`` / ``— 3 —``)。

    只认「整行就是页码」—— 4 位以上(年份)、带小数点的行都不是页码。
    代码/公式/脚注/注释块整体跳过(里面的数字是内容)。
    """
    return _filter_lines(ir, rule_id="page_numbers", first_id=first_id,
                         drop_line=is_page_number_line)


# ================================================================ running_heads

#: 页眉页脚判定参数(缺省值;与 legacy 相同)
RUNNING_HEAD_MIN_REPEATS = 3
RUNNING_HEAD_MAX_LEN = 40


def propose_running_heads(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """剔除页眉页脚重复行:同一短行全文出现 ≥3 次。

    保守之处(与 legacy 同款):只删「重复 且 短 且 无句末标点 且 非块结构」的行;
    目录条目行与注释行是有内容的行,不参与;单次出现的短行一律保留。
    局限:同一页内重复出现的短句(如反复出现的口号)也会被删,可用
    ``--clean-disable running_heads`` 关闭。
    """
    from collections import Counter

    blocks = [b for b in ir.editable_blocks
              if b.kind not in _ATOMIC_KINDS and not _is_page_marker(b)]
    counts: Counter[str] = Counter()
    for blk in blocks:
        for line in blk.text.split("\n"):
            s = line.strip()
            if _is_running_head_candidate(s, RUNNING_HEAD_MAX_LEN):
                counts[s] += 1
    dupes = {s for s, c in counts.items() if c >= RUNNING_HEAD_MIN_REPEATS}
    if not dupes:
        return RuleProposal("running_heads", ())
    return _filter_lines(ir, rule_id="running_heads", first_id=first_id,
                         drop_line=lambda line: line.strip() in dupes)


# ================================================================ trailing_spaces

_TRAILING_RE = re.compile(r"[ \t]+$")


def _strip_trailing(line: str) -> str:
    """去掉行尾空白,但**行尾恰好两个空格**要留着 —— 那是 Markdown 硬换行。

    为什么单独开这个例外(P6 修):硬换行是**内容**(读者要看到这里断行),不是
    噪声。旧引擎把它当空白一并 strip,然后只对"仍判定为诗行"的行重新补上,于是:

      - 正文里**有意**的硬换行被悄悄抹掉(渲染成空格,断行消失);
      - 补回来的那些行在第二轮清理时又被 strip 一次、再补一次 —— 每次运行都产
        出两条互相抵消的 Patch,「第二遍零改动」这条幂等约束就断了
        (实测 ``白日依山尽  `` + ``黄河入海流`` 在第二遍触发
        ``trailing_spaces`` 与 ``join_lines`` 各一条)。

    只认「恰好两个空格」:三个以上空格是「硬换行 + 多余空白」,整段 strip 掉
    (与旧引擎同结果,也避免把制表符混进来的行当硬换行)。
    """
    if line.endswith(HARD_BREAK) and not line[:-len(HARD_BREAK)].endswith((" ", "\t")):
        return line
    return _TRAILING_RE.sub("", line)


def propose_trailing_spaces(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """行尾空白清理(legacy 第 10 步的无条件动作,但保留硬换行)。

    为什么必须排在拼接之前:诗行硬换行就是「行尾两个空格」,若压空白排在后面,
    上一步刚补的硬换行会被它自己吃掉。
    """
    patches: list[Patch] = []
    touched = 0
    id_counter = first_id
    for blk in sorted(ir.editable_blocks, key=lambda b: b.order):
        if blk.kind in _TRAILING_SKIP_KINDS \
                or str(blk.extra.get("role") or "") in _TRAILING_SKIP_ROLES:
            continue
        lines = blk.text.split("\n")
        fixed = [_strip_trailing(line) for line in lines]
        if fixed == lines:
            continue
        touched += sum(1 for a, b in zip(lines, fixed) if a != b)
        patches.append(Patch.propose(
            patch_id=f"p{id_counter:04d}", rule_id="trailing_spaces", block=blk,
            operation="replace_text", after_text="\n".join(fixed),
            evidence=_line_evidence(blk.text[:32]),
            confidence="derived", risk="low",
        ))
        id_counter += 1
    notes = (f"行尾空白: {touched} 行",) if touched else ()
    return RuleProposal("trailing_spaces", tuple(patches), notes)


# ================================================================ dup_headings

def propose_dup_headings(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """相邻重复标题只保留一条(提取/OCR 常在页眉处重复输出同一标题)。

    只在「中间只有 ≤1 个空行、级别与文字都相同」时判定为相邻重复;
    被正文隔开的同名标题(如两章各有一个「小结」)一律保留,
    级别不同的同名标题(书名标题 + 章标题)也保留。
    """
    patches: list[Patch] = []
    removed = 0
    id_counter = first_id
    prev_key: tuple[int, str] | None = None
    prev_trailer = ir.meta.prefix

    for blk in sorted(ir.editable_blocks, key=lambda b: b.order):
        if blk.kind != "heading" or blk.level is None:
            prev_key = None
            prev_trailer = str(blk.extra.get("trailer", ""))
            continue
        key = (blk.level, blk.text.strip().lstrip("#").strip())
        if key[1] and prev_key == key and _blanks(prev_trailer) <= 1:
            patches.append(Patch.propose(
                patch_id=f"p{id_counter:04d}", rule_id="dup_headings", block=blk,
                operation="delete_block", evidence=_whole_block_evidence(blk),
                confidence="derived", risk="low",
            ))
            id_counter += 1
            removed += 1
            prev_key = None          # 连续三个同名标题也只留一个
            prev_trailer = str(blk.extra.get("trailer", ""))
            continue
        prev_key = key if key[1] else None
        prev_trailer = str(blk.extra.get("trailer", ""))
    notes = (f"重复标题: 去重 {removed} 行",) if removed else ()
    return RuleProposal("dup_headings", tuple(patches), notes)


# ================================================================ front_matter

#: 卷首页判定的阈值(全部可数,写在这里以便与判据同源审计)
FRONT_MAX_BLOCKS = 8          # 卷首页最多几块
FRONT_MAX_CHARS = 120         # 卷首页最多几个非空白字符
FRONT_MAX_LINE = 60           # 单块最长字符数(超过就不再是"版式字段",是正文)
FRONT_TITLE_SIMILARITY = 0.7  # 与书名的相似度门槛

#: 卷首页里常见的版式字段(出版社/版次/作者…):短行且无句末标点
_FRONT_FIELD_RE = re.compile(r"^(?:著|编著|主编|译|译注|编|撰|等|出版社|出版|发行|印次|版次|ISBN|定价)")


def _norm_title(text: str) -> str:
    """书名归一:去空白(含全角)、去书名号与常见间隔符 —— 只用于比相似度。"""
    text = re.sub(r"[\s\u3000]+", "", text or "")
    return re.sub(r"[《》〈〉“”‘’\"'·・:：,，.。、!！?？\-—–_/\\()（）\[\]【】]+", "", text).lower()


def _title_similarity(a: str, b: str) -> float:
    """两个书名的相似度(0~1):完全相同 1;互为子串按长度比;否则编辑相似度。"""
    import difflib

    left, right = _norm_title(a), _norm_title(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if left in right or right in left:
        ratio = max(ratio, min(len(left), len(right)) / max(len(left), len(right)))
    return ratio


def _same_title(a: str, b: str) -> bool:
    """是不是同一个书名。

    子串关系单独放行:文件名常写「巴勒斯坦问题」而封面写「巴勒斯坦问题:一部历史」,
    按编辑相似度只有 0.6,但那显然是同一本书。较短的 ≥4 字才认,避免「中国」这种
    两个字撞上「中国哲学史」。
    """
    left, right = _norm_title(a), _norm_title(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if left in right or right in left:
        return min(len(left), len(right)) >= 4
    return _title_similarity(left, right) >= FRONT_TITLE_SIMILARITY


def _heading_text(block: Block) -> str:
    return block.text.strip().lstrip("#").strip()


def _looks_like_front_field(text: str) -> bool:
    """一行"版式字段"(封面/扉页上的书名、作者、出版社…):短、无句末标点。"""
    line = text.strip()
    if not line or len(line) > 24:
        return False
    return line[-1] not in END_PUNCT


def propose_front_matter(ir: BookIR, *, first_id: int = 1,
                         title_hint: str | None = None) -> RuleProposal:
    """卷首(封面/扉页)去重:第 1 页若只是书名、作者、出版社这类版式字段,整块删除。

    为什么必须删:封面图已作为 ``cover.jpg`` 放在 EPUB 首位(见 ``batch.render_cover``),
    OCR 再把同一页识成一堆文字,读者打开后就会看到**两遍**书名与作者 —— 再加上
    ``dc:title`` 生成的标题页,一本扫描书能出现三处书名。

    判据(四条同时成立才删,缺一条只留原样):
      1. 该块属于**第 1 页**(``prov.page_idx == 0``);没有页归属证据时,退化为
         "文首连续块",且必须能在其中找到与书名高度相似(≥0.7)的标题;
      2. 整页块数 ≤ ``FRONT_MAX_BLOCKS``、非空白字符 ≤ ``FRONT_MAX_CHARS``;
      3. 没有长句:任一可见块超过 ``FRONT_MAX_LINE`` 字或带句末标点 → 判定为正文,
         整条规则放弃(宁可留一页重复,也不能吃掉第一章的开头);
      4. 页里要么有与书名相似的标题,要么全是 ≤24 字的短行(纯字段式封面)。

    只动 ``delete_block``,文字一个字节都不改 —— 删错了可以从报告的 before 里原文恢复。
    """
    blocks = [b for b in sorted(ir.editable_blocks, key=lambda b: b.order)
              if not _is_page_marker(b) and str(b.extra.get("role") or "") != "html_comment"]
    if len(blocks) < 2:
        return RuleProposal("front_matter")

    page1 = [b for b in blocks if b.prov.page_idx == 0]
    positional = False
    if page1:
        # 第 1 页 = 文首连续的一段,直到第一块明确属于后面某页。中间那些**没有页归属**
        # 的块(太短、对齐不出锚点的版式字段,如「xx 出版社」)留在段内 —— 它们物理上
        # 就在第 1 页,只是对齐阶段挂不上证据。少了这一步,封面页会只删掉书名、
        # 把作者和出版社留在正文第一屏。
        page1 = []
        for blk in blocks:
            idx = blk.prov.page_idx
            if idx is not None and idx > 0:
                break
            page1.append(blk)
    else:
        # 没有页归属(电子版 PDF / 现成 md):只能按"文首、到第一个章节标题之前"取。
        # 这条路更危险,所以额外要求必须命中书名 —— 认不出书名就不动。
        positional = True
        for blk in blocks:
            if page1 and blk.kind == "heading" and is_chapter_anchor(_heading_text(blk)):
                break
            page1.append(blk)
            if len(page1) > FRONT_MAX_BLOCKS:
                return RuleProposal("front_matter")

    if not page1 or len(page1) >= len(blocks):
        return RuleProposal("front_matter")      # 整本书都在"第 1 页":不可能,不猜

    visible = [b.text.strip() for b in page1 if b.text.strip()]
    chars = sum(len(re.sub(r"\s+", "", b.text)) for b in page1)
    if len(page1) > FRONT_MAX_BLOCKS or chars > FRONT_MAX_CHARS:
        return RuleProposal("front_matter")
    if any(len(t) > FRONT_MAX_LINE or t[-1] in END_PUNCT for t in visible):
        return RuleProposal("front_matter")
    # 封面/扉页不会写着「第一章」:第 1 页上有章节标注 → 这是正文的第一页
    # (实测:章标题独占一页时,旧判据会把它当成"字段式封面"删掉)
    if any(b.kind == "heading" and is_chapter_anchor(_heading_text(b)) for b in page1):
        return RuleProposal("front_matter")

    headings = [b for b in page1 if b.kind == "heading"]
    matched: Block | None = None
    if title_hint:
        for blk in headings:
            if _same_title(_heading_text(blk), title_hint):
                matched = blk
                break
    fields_only = all(_looks_like_front_field(t) for t in visible) and bool(visible)
    if matched is None and (not fields_only or len(page1) < 2):
        # 认不出书名、又只有孤零零一块:那更像"只排了一行标题的正文页",不删
        return RuleProposal("front_matter")
    if positional and matched is None:
        # 没有页证据又没有书名命中:文首那几行可能是序言/引文,不删
        return RuleProposal("front_matter")
    # 卷首页之后必须紧接章节标题:否则这更像是正文开头(有些书直接从第一段起)
    rest = [b for b in blocks if b.order > page1[-1].order]
    nxt = next((b for b in rest if b.kind == "heading"), None)
    if nxt is not None and not is_chapter_anchor(_heading_text(nxt)) and matched is None:
        return RuleProposal("front_matter")

    patches = []
    id_counter = first_id
    for blk in page1:
        patches.append(Patch.propose(
            patch_id=f"p{id_counter:04d}", rule_id="front_matter", block=blk,
            operation="delete_block", evidence=_whole_block_evidence(blk),
            confidence="derived", risk="low",
        ))
        id_counter += 1

    note = (f"卷首页: 删除 {len(patches)} 块(封面/扉页已在封面图上)"
            + (f", 书名取自卷首《{_heading_text(matched)}》" if matched is not None else ""))
    return RuleProposal("front_matter", tuple(patches), (note,))


# ================================================================ headings

#: 部/卷级结构标注(中文「第X部/编/卷/篇」,英文 Part/Book)
_PART_ANCHOR_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百千〇零两\d]+\s*[部编卷篇]"
    r"|(?:Part|PART|Book|BOOK)\s+[\dIVXLC]+)")
#: 章级结构标注(中文「第X章/回/节」,英文 Chapter N,数字编号,前言/附录一类)
_CHAPTER_ANCHOR_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百千〇零两\d]+\s*[章回节讲]"
    r"|(?:Chapter|CHAPTER)\s+[\dIVXLC]+"
    r"|\d{1,3}\s*[.、]\s*\S"
    r"|(?:序|序言|楔子|引言|导言|前言|后记|跋|结语|尾声|附录|致谢|参考文献|索引)\s*$)")


def is_chapter_anchor(text: str) -> bool:
    """标题文字是否带章节结构标注(只用于判定"这一行是章",不做语义猜测)。"""
    line = (text or "").strip()
    return bool(line) and bool(_PART_ANCHOR_RE.match(line) or _CHAPTER_ANCHOR_RE.match(line))


def _mode_level(levels: list[int]) -> int | None:
    """出现次数最多的级别;次数相同取更外层(数字更小)的那个。"""
    if not levels:
        return None
    counts: dict[int, int] = {}
    for lv in levels:
        counts[lv] = counts.get(lv, 0) + 1
    return min(counts, key=lambda lv: (-counts[lv], lv))


def structure_base_level(units: list[tuple[int, str]]) -> tuple[int | None, str]:
    """整本书的"章"应该落在第几级(1 = EPUB 的分卷单位)。

    返回 ``(基准级别, 依据说明)``;``None`` 表示不做层级归一 —— 拿不准就不动,
    因为平移层级会改变 EPUB 的分卷,改错的代价比不改大得多。

    取法:
      - 有「第X部/卷」(≥2 处)→ 取部级(章保持相对更深:三十章的书不该变成三十个文件);
      - 否则有「第X章/Chapter N」(≥2 处)→ 取章级;
      - 都没有 → 退化为"出现 ≥3 次的最浅级别"(最常见的一层就是章节层)。

    三道安全闸:
      - 基准 >3:整本书的层级异常,不猜;
      - 基准之上还有标题:那是书名/卷首标题,平移后会与章同级(反而制造重复标题页),
        留给 ``front_matter`` 先删,这里不动。
    """
    if not units:
        return None, "没有标题"
    if len(units) == 1:
        # 只有一条标题的短文本(单章/单节):它本身就是"章节层",该在第 1 级。
        # 这里不做"有没有书名在上头"的判断 —— 没有第二条标题可比。
        return units[0][0], "唯一标题"
    parts = [lv for lv, text in units if _PART_ANCHOR_RE.match(text)]
    chapters = [lv for lv, text in units
                if _CHAPTER_ANCHOR_RE.match(text) and not _PART_ANCHOR_RE.match(text)]
    if len(parts) >= 2:
        base, how = _mode_level(parts), "部/卷"
    elif len(chapters) >= 2:
        base, how = _mode_level(chapters), "章"
    else:
        if len(units) < 3:
            return None, "标题不足 3 个且没有章节标注"
        levels = [lv for lv, _ in units]
        lowest = min(levels)
        if len(units) <= 4:
            # 极短输入(单章/两章的小册子):层数不足以统计,「最浅的一层」就是章。
            # 这也是旧的「首个标题提升为 1」唯一还站得住的场景 —— 这里把它换成了
            # 整篇一起平移,不会出现「第一个抬成 #、其余留在 ##」的半截结构。
            base, how = lowest, "短文本最浅层"
        elif levels.count(lowest) >= 3:
            base, how = lowest, "层级频次"
        else:
            return None, "没有可识别的章节标注"
    if base is None:
        return None, "没有可识别的章节标注"
    if base > 3:
        return None, f"章节基准层级 {base} 过深"
    above = [lv for lv, _ in units if lv < base]
    if above:
        return None, f"基准之上还有 {len(above)} 个标题(书名/卷首),先交给 front_matter"
    return base, f"按{how}判定基准 {base}"


def propose_headings(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """空标题删除 + **整书层级基准归一** + 层级跳跃收敛。

    修正规则(只动 `#` 的个数与"# 后那个空格",不改标题文字):
      - 空标题(只有 `#`,没有文字)→ 删除;
      - **层级基准归一**:章落在第 2 级(或更深)时,全书标题整体上移,让"章"回到
        第 1 级(见 :func:`structure_base_level`);
      - 层级跳跃(`#` 后直接 `###`)→ 收为「上一级 +1」。

    **为什么不再"只提升首个标题"**:旧逻辑把第一个标题抬到 1 级,其余不动。OCR 常见
    形态是「书名 `#` + 章 `##`」,书名被 ``front_matter`` 删掉之后,旧逻辑会把
    *第一章* 抬成 `#`、其余章留在 `##` —— 结构比修之前更乱。改成整体平移后,
    相对深度(部→章→节)原样保留,章回到第 1 级。

    第 1 级是 EPUB 的分卷单位:pandoc 只在 1 级标题处切文件,脚注(``<aside>``)落在
    **所在章节末尾**。全书只有一个 1 级标题时,整本就一个文件,注释自然全堆到书末 ——
    这是"注释总在最后"的真正原因,不是脚注链路的问题。

    正文与层级放在**同一条 Patch** 里(``replace_text`` 允许带 ``after.level``):
    拆成两条会让"改层级"与"改文本"在应用期互相把对方的 before 校验弄失效。
    """
    patches: list[Patch] = []
    notes: list[str] = []
    dropped = leveled = 0
    id_counter = first_id

    headings = [b for b in sorted(ir.editable_blocks, key=lambda b: b.order)
                if b.kind == "heading" and b.level is not None]
    units = [(b.level, _heading_text(b)) for b in headings if _heading_text(b)]
    base, why = structure_base_level(units)
    delta = (base - 1) if base and base > 1 else 0
    if delta:
        notes.append(f"标题层级: 整体上移 {delta} 级({why})")

    prev_level: int | None = None
    shifted = 0
    for blk in headings:
        text = _heading_text(blk)
        if not text:
            patches.append(Patch.propose(
                patch_id=f"p{id_counter:04d}", rule_id="headings", block=blk,
                operation="delete_block", evidence=_whole_block_evidence(blk),
                confidence="derived", risk="low",
            ))
            id_counter += 1
            dropped += 1
            continue
        level = blk.level
        if delta:
            level = max(1, level - delta)
            if level != blk.level:
                shifted += 1
        if prev_level is not None and level > prev_level + 1:
            level = prev_level + 1
            leveled += 1
        prev_level = level
        new_text = "#" * level + " " + text
        if new_text == blk.text and level == blk.level:
            continue                       # 已经是目标形态:不产空补丁
        patches.append(Patch.propose(
            patch_id=f"p{id_counter:04d}", rule_id="headings", block=blk,
            operation="replace_text", after_text=new_text, level=level,
            evidence=_line_evidence(blk.text),
            confidence="derived", risk="low",
        ))
        id_counter += 1

    if dropped:
        notes.append(f"空标题: 删除 {dropped} 行")
    if shifted:
        notes.append(f"标题层级: 平移 {shifted} 个标题")
    if leveled:
        notes.append(f"标题层级: 收敛跳跃 {leveled} 处")
    return RuleProposal("headings", tuple(patches), tuple(notes))


# ================================================================ join_lines

@dataclass
class _Unit:
    """摊平后的单元:与 legacy 拼接状态机的 unit 一一对应。"""

    kind: str            # "text" | "atomic" | "mark"
    text: str            # text=原始行;atomic/mark=块原文
    block: Block
    line_no: int | None  # text 单元在块内的行号
    gap: int             # 单元前的空行数
    idx: int = 0


@dataclass
class _Entry:
    """状态机输出:一条 = 一组互相拼接的单元,或一个 mark(页码注释块)。

    ``marks``:本组期间出现的页码注释块(渲染顺序在 ``text`` **之后** —— 它们仍是
    独立的块,位置不动,只是不再打断拼接;见 ``_run_join_state_machine``)。
    """

    kind: str                  # "group" | "mark"
    units: list[_Unit] = field(default_factory=list)
    text: str = ""             # group:拼接后的行(mark:注释原文)
    gap: int = 0               # 本条目之前的空行数
    marks: list[_Unit] = field(default_factory=list)


def _flatten(ir: BookIR) -> list[_Unit]:
    """可编辑块 → 单元流(与逐行视角对齐)。"""
    blocks = [b for b in ir.blocks if b.editable]
    units: list[_Unit] = []
    prev_ws = ir.meta.prefix
    for blk in blocks:
        gap = _blanks(prev_ws)
        if _is_atomic(blk):
            units.append(_Unit("atomic", blk.text, blk, None, gap))
        elif _is_page_marker(blk):
            units.append(_Unit("mark", blk.text, blk, None, gap))
        else:
            for line_no, line in enumerate(blk.text.split("\n")):
                units.append(_Unit("text", line, blk, line_no,
                                   gap if line_no == 0 else 0))
        prev_ws = str(blk.extra.get("trailer", ""))
    for i, u in enumerate(units):
        u.idx = i
    return units


def _sep_between(prev_tail: str, cur_head: str) -> str:
    """中英文断行拼接:两侧均为拉丁字母时补空格(legacy 同款)。"""
    if prev_tail and cur_head and prev_tail[-1].isascii() and prev_tail[-1].isalpha() \
            and cur_head[0].isascii() and cur_head[0].isalpha():
        return " "
    return ""


def _joinable(pending_text: str, pending_is_text: bool, pending_layout: bool,
              unit: _Unit) -> bool:
    """拼接判据,逐条对应 legacy ``_join_broken_lines``。

    比 legacy 多一条本地约束:
    - ``pending_is_text``:pending 是原子块(代码/公式/脚注)时绝不拼 —— legacy
      靠 kind 字段挡住,这里的等价物必须是显式判断。

    (行内代码在 legacy 里 mask 成不带 ``<`` 包装的占位符,不构成块结构,
    行首是行内代码的行 legacy **会**拼 —— IR 版同样拼,字节一致。
    legacy 在拼接前还有一步**无条件**的空行压缩(≥3 空行 → 1),所以它眼里的
    gap 恒 ≤1;IR 版在管线里也先跑空行压缩,这里再用 ``min(gap, 1)`` 收敛出同一
    视角,拼接判定才不因源文件的多余空行而失灵。)
    """
    if not pending_is_text or unit.kind != "text":
        return False
    gap = min(unit.gap, 1)
    pt = pending_text
    text = unit.text.strip()
    if not pt or not text:
        return False
    prev_len = len(re.sub(r"\s+", "", pt))
    cur_len = len(re.sub(r"\s+", "", text))
    verse_pair = (prev_len <= VERSE_MAX_CHARS and cur_len <= VERSE_MAX_CHARS
                  and text[-1] not in END_PUNCT)
    return (
        gap <= 1
        and not _is_block_start(pt)
        and not _is_block_start(text)
        and not _is_catalog_entry(pt) and not _is_catalog_entry(text)
        and not _is_note_line(pt) and not _is_note_line(text)
        and pt[-1] not in END_PUNCT
        and text[0] not in START_PUNCT
        and prev_len >= (CROSS_BLANK_MIN_CHARS if gap >= 1 else ADJACENT_MIN_CHARS)
        and not pending_layout
        and not _is_layout_unit("text", text, gap)
        and not verse_pair
    )


def _run_join_state_machine(units: list[_Unit], *,
                            linked_ids: frozenset[str]) -> tuple[list[_Entry],
                                                                dict[str, _Unit | None]]:
    """拼接状态机的 IR 版。返回 ``(entries, last_mark_before)``。

    ``linked_ids``:有结构证据(证据层链接)的块 id 集合。跨块拼接只在「被吸收块
    自身有结构证据」时发生 —— 删除的是它,证据就必须是它的;否则删除过不了
    ``evidence.sufficient`` 关,提出来就是「正文并了、块还在」的重复内容。
    只认被吸收侧还有一条性质:replay 时对齐会重新挂链接,若靠"任一侧"放行,
    第二次运行会把上次缺证据的拼接继续做完 —— 幂等性就没了。

    **页码注释块(mark)对拼接透明** —— 它标记页边界,不是内容:

    - 注释块本身照原位置渲染(位置不动、文字不动),所以合并后的段落与它之间
      可能只差一个空行;注释在 EPUB 里是 HTML 注释,不可见,顺序变化不影响产物;
    - 但注释把「上一页最后一行」和「下一页第一行」隔开了,若不透明,每一处页界都会
      把段落切成两段(合成出的段落边界在阅读器里就是句中换行);
    - ``last_mark_before`` 记"被吸收块之前紧邻的注释块":块间空行属于被删块,
      删除后要由那个注释块接管(否则文件末尾换行、段后空行会少一截)。
    """
    entries: list[_Entry] = []
    last_mark_before: dict[str, _Unit | None] = {}
    pending: list[_Unit] | None = None
    pending_text = ""
    pending_is_text = False
    pending_layout = False
    pending_marks: list[_Unit] = []     # 上一单元之后出现、还没被哪个吸收动作认领的注释
    group_marks: list[_Unit] = []       # 本组期间出现的所有注释(渲染顺序)

    def flush() -> None:
        nonlocal pending, pending_text, pending_is_text, pending_layout
        nonlocal pending_marks, group_marks
        if pending is None:
            return
        # 拼接过程会把每个文本行 strip 后再重建(源文件里图片行、目录行的行尾
        # 空白就是这么丢的)—— IR 版按同一语义 strip,才能逐字节对齐
        if len(pending) == 1 and pending[0].kind == "text":
            text = pending[0].text.strip()
        else:
            text = pending_text               # 拼接结果(判据本来就跑在 strip 视图上)
        entries.append(_Entry("group", pending, text, pending[0].gap,
                              list(group_marks)))
        pending, pending_is_text, pending_layout = None, False, False
        pending_marks, group_marks = [], []

    for u in units:
        if u.kind == "mark":
            if pending is None:
                # 前面没有可拼的组:注释就是独立条目(原样渲染,不产生 Patch)
                entries.append(_Entry("mark", [u], u.text, u.gap))
            else:
                pending_marks.append(u)
                group_marks.append(u)
            continue
        u_text = u.text.strip() if u.kind == "text" else u.text
        if pending is None:
            pending = [u]
            pending_text = u_text
            pending_is_text = u.kind == "text"
            pending_layout = (u.kind == "text"
                              and _is_layout_unit("text", u_text, u.gap))
            continue
        same_block = pending[-1].block.id == u.block.id
        if (same_block or u.block.id in linked_ids) \
                and _joinable(pending_text, pending_is_text, pending_layout, u):
            if not same_block:
                # 跨块吸收:这段路上最后那个注释块接管空行归属
                last_mark_before[u.block.id] = pending_marks[-1] if pending_marks else None
                pending_marks = []
            pending_text += _sep_between(pending_text, u_text) + u_text
            pending.append(u)
            pending_layout = False
        else:
            flush()
            pending = [u]
            pending_text = u_text
            pending_is_text = u.kind == "text"
            pending_layout = (u.kind == "text"
                              and _is_layout_unit("text", u_text, u.gap))
    flush()
    return entries, last_mark_before


def _verse_mark(entries: list[_Entry]) -> int:
    """给连续短行补硬换行(legacy ``_mark_verse_lines`` 的移植)。

    在**拼接后**的条目流上做(与 legacy 在 join 之后跑同一判定一致)。
    原子块整体跳过;页码注释行对连续性透明(一首诗跨页时,页界不该让分行断掉),
    返回补了几行。
    """
    lines: list[dict] = []

    def push(text: str, e_idx: int | None, ln: int | None, *,
             skip: bool, mark: bool) -> None:
        lines.append({"text": text, "entry": e_idx, "line": ln,
                      "skip": skip, "mark": mark})

    for e_idx, entry in enumerate(entries):
        if e_idx > 0:
            for _ in range(entry.gap):
                push("", None, None, skip=True, mark=False)
        skip = entry.kind == "mark" or entry.units[0].kind == "atomic"
        for ln, line in enumerate(entry.text.split("\n")):
            push(line, e_idx, ln, skip=skip, mark=entry.kind == "mark")
        for m in entry.marks:                      # 组内注释块:渲染顺序在其后
            for _ in range(m.gap):
                push("", None, None, skip=True, mark=False)
            for ln, line in enumerate(m.text.split("\n")):
                push(line, None, ln, skip=True, mark=True)

    marked = 0
    i = 0
    while i < len(lines):
        rec = lines[i]
        if rec["skip"] or not _keep_line_break(rec["text"]):
            i += 1
            continue
        run = [i]
        cursor = i
        while True:
            k = cursor + 1
            while k < len(lines) and lines[k]["mark"]:
                k += 1
            if k < len(lines) and not lines[k]["skip"] \
                    and _keep_line_break(lines[k]["text"]):
                run.append(k)
                cursor = k
            else:
                break
        if len(run) >= 2:
            for k in run[:-1]:
                lines[k]["text"] = lines[k]["text"].rstrip() + HARD_BREAK
                marked += 1
        i = cursor + 1
    for e_idx, entry in enumerate(entries):
        if entry.kind != "group":
            continue
        own = [rec["text"] for rec in lines
               if rec["entry"] == e_idx and not rec["mark"]]
        if own:
            entry.text = "\n".join(own)
    return marked


def _absorptions(entries: list[_Entry]) -> dict[str, str]:
    """跨块拼接的吸收关系:被吸收块 id → 吸收者(链首块)id。"""
    root_of: dict[str, str] = {}
    for entry in entries:
        if entry.kind != "group" or len(entry.units) < 2:
            continue
        root = entry.units[0].block.id
        for u in entry.units[1:]:
            if u.block.id != root:
                root_of[u.block.id] = root
    return root_of


def _boundary_snippet(entries: list[_Entry], block_id: str) -> str:
    """该块参与的第一个跨块拼接的拼接结果片段(evidence.matched 用)。"""
    for entry in entries:
        if entry.kind != "group" or len(entry.units) < 2:
            continue
        ids = [u.block.id for u in entry.units]
        if block_id in ids and len(set(ids)) > 1:
            return entry.text[:32]
    return ""


def propose_join_lines(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """join_lines(+ 诗行硬换行)的候选提案。"""
    units = _flatten(ir)
    blocks = [b for b in ir.blocks if b.editable]

    # 有结构证据(证据层链接)的块:被吸收块自己必须有,删除才立得住(且幂等)
    linked_ids = frozenset(
        b.id for b in blocks if _linked_evidence(ir, b) is not None)

    entries, last_mark_before = _run_join_state_machine(units, linked_ids=linked_ids)
    # 假设跑(不设证据门槛):找出「本可以拼但被吸收块缺证据」的块,留 suggested 建议
    hypothetical, _ = _run_join_state_machine(
        units, linked_ids=frozenset(b.id for b in blocks))
    blocked = {bid for bid in _absorptions(hypothetical) if bid not in linked_ids}

    marked = _verse_mark(entries)
    root_of = _absorptions(entries)

    # 按块重组新文本:同一 owner 的**连续**条目合成一段(条目之间的分隔只在
    # 同 owner 内部落进 text;跨 owner 的空隙由 trailer 表达)。
    # 页码注释条目**不参与**:它仍是独立块、位置不动,文字不进相邻块。
    new_text: dict[str, str] = {}
    run_owner: str | None = None
    pieces: list[str] = []
    for entry in entries:
        if entry.kind == "mark":
            continue
        owner = entry.units[0].block.id
        owner = root_of.get(owner, owner)
        if owner != run_owner:
            if run_owner is not None:
                new_text[run_owner] = "".join(pieces)
            run_owner, pieces = owner, []
        elif pieces:
            pieces.append("\n" * (entry.gap + 1))
        pieces.append(entry.text)
    if run_owner is not None:
        new_text[run_owner] = "".join(pieces)

    # 吸收者的块间空行 = 最后一个被吸收块的 trailer,并按空行压缩语义收敛
    # (≥3 空行 → 1):拼接后的段落与下文之间,压缩后的空行才是最终形态。
    # 若吸收路径上有页码注释,空行归属交给**紧邻被删块的那个注释块**:注释块留在
    # 原位,被删块的 trailer 只有它能接着。
    def _compress_trailer(trailer: str) -> str:
        if trailer == "":
            return ""
        return "\n" * (min(_blanks(trailer), 1) + 1)

    blocks_by_id = {b.id: b for b in blocks}
    new_trailer: dict[str, str] = {}
    absorbed_by_root: dict[str, list[str]] = {}
    for absorbed, root in root_of.items():
        absorbed_by_root.setdefault(root, []).append(absorbed)
    for root, absorbed in absorbed_by_root.items():
        last = max(absorbed, key=lambda bid: blocks_by_id[bid].order)
        mark = last_mark_before.get(last)
        carrier_id = mark.block.id if mark is not None else root
        new_trailer[carrier_id] = _compress_trailer(
            str(blocks_by_id[last].extra.get("trailer", "")))

    id_counter = first_id

    def next_id() -> str:
        nonlocal id_counter
        pid = f"p{id_counter:04d}"
        id_counter += 1
        return pid

    patches: list[Patch] = []
    notes: list[str] = []
    emitted: dict[str, str] = {}
    for blk in blocks:
        if blk.id in root_of:
            dep = emitted.get(root_of[blk.id])
            if dep is None:                   # 理论不可达(吸收者必在更早的块)
                continue
            # 删除的证据 = 被吸收块自己的链接(放行条件已保证存在)
            patches.append(_dc_replace(
                Patch.propose(
                    patch_id=next_id(), rule_id="join_lines", block=blk,
                    operation="delete_block",
                    evidence=_linked_evidence(ir, blk),
                    confidence="heuristic", risk="high",
                ),
                depends_on=dep,
                validation=Validation(notes=(f"由 {root_of[blk.id]} 拼接吸收,"
                                             f"依赖 {dep} 先成功",)),
            ))
            continue
        text = new_text.get(blk.id, blk.text)
        trailer = new_trailer.get(blk.id)
        if trailer is not None and trailer == str(blk.extra.get("trailer", "")):
            trailer = None
        if text == blk.text and trailer is None:
            continue
        joined = text != blk.text
        patch = Patch.propose(
            patch_id=next_id(), rule_id="join_lines", block=blk,
            operation="replace_text", after_text=text,
            evidence=Evidence(kind="text_rule",
                              matched=_boundary_snippet(hypothetical, blk.id)
                              or text[:32],
                              source="text_rule"),
            confidence="heuristic", risk="medium" if joined else "low",
        )
        if trailer is not None:
            patch = _dc_replace(patch, after=_dc_replace(patch.after, trailer=trailer))
        emitted[blk.id] = patch.patch_id
        patches.append(patch)

    # 缺证据的跨块拼接:留 suggested 的删除建议,让「为什么没拼」可审计
    for blk in blocks:
        if blk.id in blocked:
            patches.append(Patch.propose(
                patch_id=next_id(), rule_id="join_lines", block=blk,
                operation="delete_block",
                evidence=Evidence(kind="text_rule", matched=blk.text.strip()[:32],
                                  source="text_rule"),
                confidence="heuristic", risk="high",
            ).with_outcome(
                "suggested", skip_reason="evidence_insufficient",
                validation=Validation(before_ok=True, after_ok=False, notes=(
                    "跨块拼接缺少结构证据(该块没有对齐到证据层),只记建议",)),
            ))

    if marked:
        notes.append(f"诗行硬换行 {marked} 行")
    if root_of:
        notes.append(f"跨块拼接 {len(root_of)} 处(删除依赖前置替换成功)")
    if blocked:
        notes.append(f"跨块拼接 {len(blocked)} 处因缺证据降级为建议")
    return RuleProposal("join_lines", tuple(patches), tuple(notes))


# ================================================================ 空格规则骨架

def _fix_outside_inline(line: str, fix) -> str:
    """把 fix 只作用在行内代码片段之外(行内代码里的空格是代码的一部分)。"""
    parts: list[str] = []
    last = 0
    for m in INLINE_CODE_RE.finditer(line):
        parts.append(fix(line[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(fix(line[last:]))
    return "".join(parts)


def _line_records(ir: BookIR) -> list[dict]:
    """摊平成行记录(空格规则用):含块边界上的虚拟空行。

    每条记录:{text, block, line_no, blank_like, skip}。
    ``blank_like``:空行或页码注释(``_blank`` 的语义);``skip``:不参与改写。
    """
    records: list[dict] = []
    blocks = [b for b in ir.blocks if b.editable]
    prev_ws = ir.meta.prefix
    for blk in blocks:
        for _ in range(_blanks(prev_ws)):
            records.append({"text": "", "block": None, "line_no": None,
                            "blank_like": True, "skip": True})
        prev_ws = str(blk.extra.get("trailer", ""))
        if _is_atomic(blk):
            for _line in blk.text.split("\n"):
                records.append({"text": _line, "block": blk, "line_no": None,
                                "blank_like": False, "skip": True})
            continue
        if _is_page_marker(blk):
            for _line in blk.text.split("\n"):
                records.append({"text": _line, "block": blk, "line_no": None,
                                "blank_like": True, "skip": True})
            continue
        for line_no, line in enumerate(blk.text.split("\n")):
            records.append({"text": line, "block": blk, "line_no": line_no,
                            "blank_like": False, "skip": False})
    return records


def _block_text_from(records: list[dict], blk: Block) -> str | None:
    """按行记录重组块的文本(只换被改的行;没有改过 = None)。"""
    src = blk.text.split("\n")
    repl: dict[int, str] = {}
    for rec in records:
        if rec["block"] is not None and rec["block"].id == blk.id \
                and rec["line_no"] is not None and not rec["skip"]:
            repl[rec["line_no"]] = rec["text"]
    if not repl:
        return None
    return "\n".join(repl.get(i, s) for i, s in enumerate(src))


def _space_patches(ir: BookIR, *, rule_id: str, first_id: int, line_fix,
                   skip_line) -> RuleProposal:
    """ocr_spaces / cjk_spaces 共用骨架:逐行改写 → 每个动了块的 replace_text。"""
    records = _line_records(ir)
    touched: dict[str, str] = {}
    for i, rec in enumerate(records):
        blk = rec["block"]
        if blk is None or rec["skip"]:
            continue
        if skip_line(rec["text"], i, records):
            continue
        fixed = _fix_outside_inline(rec["text"], line_fix)
        if fixed != rec["text"]:
            touched.setdefault(blk.id, rec["text"].strip()[:32])
            rec["text"] = fixed
    patches: list[Patch] = []
    id_counter = first_id
    for blk in (b for b in ir.blocks if b.editable):
        if blk.id not in touched:
            continue
        new_text = _block_text_from(records, blk)
        if new_text is None or new_text == blk.text:
            continue
        patches.append(Patch.propose(
            patch_id=f"p{id_counter:04d}", rule_id=rule_id, block=blk,
            operation="replace_text", after_text=new_text,
            evidence=Evidence(kind="text_rule", matched=touched[blk.id],
                              source="text_rule"),
            confidence="heuristic", risk="low",
        ))
        id_counter += 1
    return RuleProposal(rule_id, tuple(patches), ())


# ================================================================ 空行压缩

#: legacy ``clean_markdown`` 第 5 步与第 9 步的同一条表达式(两处无条件执行)。
#: 语义:**连续 3 个及以上换行 → 2 个换行**(即 ≥2 个空行 → 1 个空行)。
#: 注意它只认**连续**换行:``"\n \n\n"``(空行里夹了空格)不匹配,IR 版用同一个
#: 正则,不"顺手"多压一点。
BLANK_RUN_RE = re.compile(r"\n{3,}")


def compress_blank_runs(ws: str) -> str:
    """空行压缩的纯函数(与 legacy 同一正则);供测试直接对拍。"""
    return BLANK_RUN_RE.sub("\n\n", ws)


def propose_blank_lines(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """连续空行压缩(legacy 第 5/9 步的无条件步骤)。

    改成 Patch 之后它仍然**不是启发式**:判据是"这里有几个连续换行",不是猜测,
    所以 ``confidence="derived"`` —— 默认运行也会应用,但仍然每条可撤销、可审计。

    作用范围刻意收窄到**块间空白**``extra['trailer']``:

    - 块的 ``text`` 一律不动 —— 代码围栏、表格、脚注、公式、诗行、标题内部的
      换行都是**内容**,legacy 也是靠掩码保护它们的(IR 版没有掩码,于是干脆
      不进这个作用域)。段间分隔所依赖的**单个空行**不受影响(正则最少命中 3 连换行);
    - 页码注释块自带的页界信息在 ``extra['page']`` 里,压缩它后面的空行不动它;
    - 原子块(代码/表格/脚注/公式)**后面**的空行照压 —— 掩码后的占位符也是一行,
      它后面的空行同样被压。

    文首空白 ``meta.prefix`` 不在任何块的 ``trailer`` 里,用文档级 ``set_prefix``
    表达:拼接重建会把文首空白**整段丢掉**,IR 版照同一语义置空。
    """
    notes: list[str] = []
    patches: list[Patch] = []
    id_counter = first_id

    prefix = ir.meta.prefix
    if prefix:
        patches.append(Patch.propose_prefix(
            patch_id=f"p{id_counter:04d}", rule_id="blank_lines",
            before_text=prefix, after_text="",
            evidence=Evidence(kind="structural", matched=repr(prefix)[:32],
                              source="md_block"),
            confidence="derived", risk="low",
        ))
        notes.append(f"文首空白 {prefix!r} → ''(与拼接重建同语义)")
        id_counter += 1

    for blk in sorted(ir.editable_blocks, key=lambda b: b.order):
        # 多行 HTML 注释:注释不是结构块,内部空行与行尾空白 legacy 照样处理;
        # IR 版把注释当**原子块**,只压它后面的空行。这一处差异**保留**:
        # 注释内容不参与渲染,压不压都不影响产物语义,而"块 text 一律不动"是
        # 代码围栏/表格/脚注/公式能安全共存的前提(它们内部的换行同样是内容)。
        # 不静默:能压的时候在 notes 里报出来,让读报告的人知道这里与旧行为不同。
        if str(blk.extra.get("role") or "") == "html_comment" \
                and BLANK_RUN_RE.search(blk.text):
            notes.append(
                f"注释块 {blk.id} 内部含 3+ 连续换行:保留原字节"
                f"(已知差异,不影响渲染)"
            )
        current = str(blk.extra.get("trailer", ""))
        compressed = compress_blank_runs(current)
        if compressed == current:
            continue
        patch = Patch.propose(
            patch_id=f"p{id_counter:04d}", rule_id="blank_lines", block=blk,
            operation="replace_text", after_text=blk.text,
            evidence=Evidence(kind="structural", matched=blk.text.strip()[:32],
                              source="md_block"),
            confidence="derived", risk="low",
        )
        patch = _dc_replace(
            patch,
            after=_dc_replace(patch.after, trailer=compressed),
            validation=Validation(
                notes=(f"块间空行 {current.count(chr(10))} 个换行 → "
                       f"{compressed.count(chr(10))} 个(≥3 → 2)",),
            ),
        )
        patches.append(patch)
        id_counter += 1
    return RuleProposal("blank_lines", tuple(patches), tuple(notes))


# ================================================================ ocr_spaces

def _ocr_line_fix(line: str) -> str:
    frag = rf"[A-Za-z]{{1,{OCR_MAX_FRAG}}}"
    pattern = re.compile(
        rf"(?<![A-Za-z]){frag}(?: {frag}){{{OCR_MIN_FRAGS - 1},}}(?![A-Za-z])")

    def repl(m: re.Match[str]) -> str:
        parts = m.group(0).split(" ")
        if sum(1 for p in parts if len(p) <= OCR_MIN_SHORT) < OCR_MIN_SHORT:
            return m.group(0)
        return "".join(parts)

    return pattern.sub(repl, line)


def _ocr_skip(line: str, _i: int, _records: list) -> bool:
    cjk = re.compile(rf"[{CJK_CHARS}]")
    return not cjk.search(line) or _is_structure_line(line)


def propose_ocr_spaces(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """OCR 异常空格合并(``Py Mu PDF`` → ``PyMuPDF``)。"""
    return _space_patches(ir, rule_id="ocr_spaces", first_id=first_id,
                          line_fix=_ocr_line_fix, skip_line=_ocr_skip)


# ================================================================ cjk_spaces

_CJK_CLASS = rf"[{CJK_CHARS}]"

_CJK_FIXES = (
    (re.compile(rf"(?<={_CJK_CLASS}) (?={_CJK_CLASS})"), ""),
    (re.compile(rf"(?<={_CJK_CLASS}) (?=\d)"), ""),
    (re.compile(rf"(?<=\d) (?={_CJK_CLASS})"), ""),
    (re.compile(rf"(?<={_CJK_CLASS}) (?=<[^>]+>)"), ""),
    (re.compile(r"(</?[a-zA-Z]{1,5}>) (?=[\u4e00-\u9fff])"), r"\1"),
    (re.compile(rf"(?<={_CJK_CLASS}) (?=[()])"), ""),
    (re.compile(rf"(?<=[()]) (?={_CJK_CLASS})"), ""),
)


def _cjk_line_fix(line: str) -> str:
    for pattern, sub in _CJK_FIXES:
        line = pattern.sub(sub, line)
    return line


def _cjk_skip(line: str, i: int, records: list) -> bool:
    """与 ``_map_body_lines`` 一致的跳过判据。"""
    if _is_structure_line(line) or _is_verse_line(line):
        return True
    n = len(records)

    def blank_like(j: int) -> bool:
        return not (0 <= j < n) or records[j]["blank_like"]

    if blank_like(i - 1) and blank_like(i + 1) and _looks_like_layout_line(line):
        return True                          # 独立成段的版式行(版权页字段)
    return False


def propose_cjk_spaces(ir: BookIR, *, first_id: int = 1) -> RuleProposal:
    """中文排版空格修正(汉字-汉字、汉字-数字之间)。"""
    return _space_patches(ir, rule_id="cjk_spaces", first_id=first_id,
                          line_fix=_cjk_line_fix, skip_line=_cjk_skip)


# ================================================================ images(只校验)

_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def check_image_refs(md: str, images_dir) -> tuple[str, ...]:
    """图片引用存在性校验(只报结论,不改正文)。

    返回给人看的问题描述(写进报告 ``issues``);``images_dir`` 为 None 时跳过
    (纯文本输入没有 images/ 目录,不报"缺失")。

    **目录不存在不等于"没有图片可查"**:正文引用了 ``images/x.png`` 而整个
    ``images/`` 不在磁盘上,恰恰是最该报的"整批缺失"(提取失败 / work 目录被清 /
    md 与图片分家)。此前遇到目录不存在就直接返回空,于是"缺一张报、整批缺不报"——
    严重程度被倒置,而且完全静默:清理报告 issues 为空,只有 EPUB 阶段的校验才发现。
    所以目录不存在时按"现有图片为空集"继续判,而不是跳过。
    """
    from pathlib import Path

    if images_dir is None:
        return ()
    root = Path(images_dir)
    existing = ({p.name for p in root.iterdir() if p.is_file()}
                if root.is_dir() else set())
    issues: list[str] = []
    for m in _IMAGE_REF_RE.finditer(md):
        ref = m.group(1)
        name = Path(ref).name
        if name not in existing and not Path(ref).is_absolute():
            issues.append(f"图片引用缺失: {ref}")
    return tuple(issues)


# ================================================================ 驱动

_PROPOSERS = {
    "page_numbers": propose_page_numbers,
    "running_heads": propose_running_heads,
    "front_matter": propose_front_matter,
    "cjk_spaces": propose_cjk_spaces,
    "blank_lines": propose_blank_lines,
    "trailing_spaces": propose_trailing_spaces,
    "join_lines": propose_join_lines,
    "ocr_spaces": propose_ocr_spaces,
    "dup_headings": propose_dup_headings,
    "headings": propose_headings,
}

#: 需要额外上下文(书名)的规则:其余规则一律只吃 ``ir`` —— 不做语义猜测
_CONTEXT_RULES = frozenset({"front_matter"})


def propose_rules(ir: BookIR, rule_ids: tuple[str, ...] | None = None, *,
                  allow_apply: bool = False,
                  allow_rules: frozenset[str] = frozenset(),
                  options: dict | None = None,
                  title_hint: str | None = None,
                  clock=None) -> tuple[BookIR, list]:
    """按管线顺序连续提案并应用(每条规则一次 apply、一份报告)。

    ``allow_apply`` 传给 ``apply_patches.allow_heuristic_apply``(整批放行,库/测试用);
    ``allow_rules`` 是**按规则**放行 ``heuristic`` 候选的集合 —— 正式流程用
    ``clean.policy.auto_apply_rules()`` 生成,比整批放行精确。
    ``options`` 只进报告的 ``run.options``(报告要能自证"这次开着哪些开关")。

    ``clock``:取时刻的可注入来源(默认 ``time.time``)。**必须**由调用方注入或用
    默认时钟,不能把起止时刻写死 —— 报告是审计依据,说不出"这次跑的起止时刻"
    就没法回答"这一份报告是哪一次运行产出的"(P8:此前恒为 0)。

    ``title_hint``:书名(文件名/元数据推断)。只有 ``front_matter`` 用它判断
    "这一页上的大标题是不是书名";其它规则不接受这个参数(不做语义猜测)。

    返回 ``(final_ir, reports)``;patch_id 跨规则连续分配。
    """
    from clean.engine import apply_patches

    if rule_ids is None:
        rule_ids = PIPELINE_ORDER
    clock = clock or time.time
    current = ir
    reports = []
    next_id = 1
    for rule_id in rule_ids:
        proposer = _PROPOSERS.get(rule_id)
        if proposer is None:
            if rule_id in NON_PATCH_RULES:
                continue            # bold/images 不是文本改写规则,不产 Patch
            raise ValueError(f"未实现的规则: {rule_id!r}(已实现: "
                             f"{', '.join(_PROPOSERS)})")
        extra = {"title_hint": title_hint} if rule_id in _CONTEXT_RULES else {}
        proposal = proposer(current, first_id=next_id, **extra)
        if not proposal.patches:
            continue
        # 起止时刻交给 apply_patches 用 clock 取:这里若传 started_at=0/finished_at=0,
        # 就把它"钉死"成 0(clock 形同虚设)—— 正式流程的报告时间戳恒为 0 正是这么来的。
        kwargs = {"allow_heuristic_apply": allow_apply, "allow_rules": allow_rules,
                  "options": options, "rules_enabled": rule_ids,
                  "clock": clock}
        result = apply_patches(current, proposal.patches, **kwargs)
        reports.append(result.report)
        current = result.ir
        next_id += len(proposal.patches)
    return current, reports


def collect_notes(rule_ids: tuple[str, ...] | None = None) -> dict[str, str]:
    """规则 → 一句话说明(报告/调试用;避免把 docstring 再抄一遍)。"""
    return {rule_id: (fn.__doc__ or "").strip().splitlines()[0]
            for rule_id, fn in _PROPOSERS.items()}
