"""Layer1 ↔ Layer2 对齐(阶段 B §E.5 不变量 4 / §E.6)。

对齐只做一件事:把「Markdown 里的一段正文」和「content_list 里的那条结构事实」
配起来,从而把**页号**与**bbox**这些证据挂到可编辑块上。

为什么坚持"单调"而不是"全局最近匹配":OCR/转换产物的块序与 md 的段序都按阅读
顺序排列,一旦匹配位置倒退,说明两边的顺序已经不一致(重复段落、云端拼接差异、
md 被脚注重建改写过),此时**任何**匹配都不可信。所以:

  - 匹配游标只向前走,不允许回退;
  - 配不上的块 ``page_idx`` 保持 ``None`` 并计数,**不猜**;
  - 两个已定页的邻块之间夹着的未定页块,只有在两侧页号**相同时**才继承
    (阅读顺序不变 → 中间那些块必然同页),置信度 ``derived``,来源 ``interval``。

这条「宁可空着也不填错」的取舍是阶段 B 决策 7 的直接落地:页归属缺失会被
``content_page_attribution_missing`` 报出来,而不是被估算掩盖。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from ir.model import Block, Link

#: 锚点长度:取规范化文本的前 N 个字符比较。24 个汉字足以区分同页段落,
#: 又不会因为段末被脚注标记打断而失配。
ANCHOR_CHARS = 24

#: 前缀匹配的最低共同长度(第二档匹配,容忍"md 合并了多条证据块")
MIN_PREFIX_CHARS = 12

#: 按块类型放宽锚点门槛的依据:
#:   - 标题通常很短("美以特殊关系"只有 6 字),但标题本身在书内重复率低,
#:     4 字足以区分;仍然要求单调,所以"同一句话在别页复现"不会把游标带回去。
#:   - 脚注定义带 ``[^n]:`` 标记,去掉标记后与 ``page_footnote`` 的正文同源。
MIN_ANCHOR_BY_KIND: dict[str, int] = {"heading": 4, "footnote": MIN_PREFIX_CHARS}

#: 与 ``parse._FOOTDEF_RE`` 同义(这里只需要"去掉标记"这一件事)
_FOOTDEF_STRIP_RE = re.compile(r"^\[\^[^\]\s]+\]:\s*")


def normalize(text: str) -> str:
    """去掉所有空白(对齐只在"有内容的字符"上比较)。"""
    return re.sub(r"\s+", "", text)


def _image_name(ref: str) -> str:
    return Path(ref.replace("\\", "/")).name


def block_anchor(block: Block, *, chars: int = ANCHOR_CHARS,
                 enforce_min: bool = True) -> str:
    """块的对齐锚点;没有区分度的块返回空串(不参与匹配)。

    图片块按**文件名**对齐(MinerU/PaddleOCR 的 ``img_path`` 与 md 引用同源);
    标题去掉 ``#`` 标记、脚注定义去掉 ``[^n]:`` 标记再比 —— 标记只存在于 md 一侧,
    带着它比永远配不上。

    ``enforce_min=False`` 用于**证据侧**:门槛是给"用来找人的那一方"设的
    (md 块太短就没有区分度),证据侧只要有内容就能被找到;两边用同一套门槛会让
    短标题永远配不上。
    """
    if block.kind == "image":
        ref = str(block.extra.get("image_ref") or block.extra.get("img_path") or "")
        return _image_name(ref) if ref else ""
    text = block.text
    if block.kind == "heading":
        text = text.lstrip("#").strip()
    elif block.kind == "footnote":
        text = _FOOTDEF_STRIP_RE.sub("", text)
    norm = normalize(text)
    if not enforce_min:
        return norm[:chars] if norm else ""
    minimum = MIN_ANCHOR_BY_KIND.get(block.kind, MIN_PREFIX_CHARS)
    return norm[:chars] if len(norm) >= minimum else ""


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def align_blocks(editable: Sequence[Block], evidence: Sequence[Block],
                 *, chars: int = ANCHOR_CHARS) -> tuple[tuple[Block, ...], tuple[Link, ...], dict]:
    """把证据挂到可编辑块上,返回 ``(新可编辑块, links, stats)``。

    不修改入参(Block 是 frozen 的):返回的是替换过 ``prov`` 的新块。
    """
    blocks = list(editable)
    stats = {
        "evidence_available": bool(evidence),
        "anchor_hits": 0,
        "containment_hits": 0,
        "short_attributed": 0,
        "interval_attributed": 0,
        "unmatched": 0,
        "out_of_order": 0,
        "page_attribution_missing": 0,
    }
    if not evidence:
        # 没有证据层:不产生链接,也不声称页归属。缺失数照实计,便于 P5 发现
        # "某后端根本没给结构化结果"。
        stats["page_attribution_missing"] = sum(1 for b in blocks if b.page_idx is None)
        return tuple(blocks), (), stats

    anchors = [block_anchor(b, chars=chars, enforce_min=False) for b in evidence]
    norms = [normalize(b.text) for b in evidence]
    links: list[Link] = []
    claimed: dict[int, int] = {}          # editable 序号 → page_idx
    hits: dict[int, int] = {}             # editable 序号 → 命中的证据序号
    cursor = 0

    for pos, block in enumerate(blocks):
        key = block_anchor(block, chars=chars)
        if not key:
            continue
        hit = -1
        how = ""
        # 第一档:锚点相等(最常见)
        for j in range(cursor, len(evidence)):
            if anchors[j] and anchors[j] == key:
                hit, how = j, "anchor"
                break
        # 第二档:前缀匹配(md 把多条证据块合并成一段,或证据块切得更细)
        if hit < 0:
            for j in range(cursor, len(evidence)):
                if _common_prefix(key, norms[j]) >= MIN_PREFIX_CHARS:
                    hit, how = j, "containment"
                    break
        if hit < 0:
            # 有锚点却一条也配不上:可能是顺序冲突,也可能是 md 已被改写(脚注编号)。
            # 都不猜 —— 保持 None,计入 unmatched。
            stats["unmatched"] += 1
            continue
        if hit < cursor:                      # 理论上不可达(游标只前进)
            stats["out_of_order"] += 1
            continue
        ev = evidence[hit]
        if ev.page_idx is None:
            cursor = hit + 1
            stats["unmatched"] += 1
            continue
        source = ev.prov.page_idx_source or "backend"
        blocks[pos] = block.with_page(ev.page_idx, source)
        claimed[pos] = ev.page_idx
        links.append(Link(block_id=block.id, evidence_id=ev.id,
                          confidence="derived", how=how))
        hits[pos] = hit
        stats["anchor_hits" if how == "anchor" else "containment_hits"] += 1
        cursor = hit + 1

    stats["short_attributed"] = _attribute_short(blocks, evidence, hits, claimed, links)
    stats["interval_attributed"] = _fill_intervals(blocks, claimed)
    stats["page_attribution_missing"] = sum(1 for b in blocks if b.page_idx is None)
    if links:
        # 链接按**块顺序**放出:窗口匹配是第二轮补的,追加在尾部会让
        # ``links_monotonic``(§E.5 不变量 4)读成"顺序乱了"。
        order_of = {b.id: b.order for b in blocks}
        links.sort(key=lambda link: order_of.get(link.block_id, 0))
    return tuple(blocks), tuple(links), stats


#: 短块(归一化后不足 ``MIN_PREFIX_CHARS`` 字)参与匹配的下限:
#: 1 个字没有判别力(「我」「他」满页都是),从 2 字起才可能唯一。
_SHORT_MIN_CHARS = 2


def _attribute_short(blocks: list[Block], evidence: Sequence[Block],
                     hits: dict[int, int], claimed: dict[int, int],
                     links: list[Link]) -> int:
    """短块在**已匹配区间**内唯一命中证据时归还页号(跨页续行的唯一出路)。

    为什么需要:``block_anchor`` 对正文块要求归一化后 ≥ ``MIN_PREFIX_CHARS`` 字才有
    锚点,不足的整块不参与匹配 —— 于是「跨页被切开的短续行」永远拿不到页号,而它们
    正是 ``join_lines`` 想拼接的对象;没有页归属,跨块删除只能降级为建议(见
    ``clean.rules._linked_evidence``)。实测一本 319 页的扫描书:27 条拼接候选全部
    卡在这里,跨页断行一处都没连上。

    判据刻意窄:短块只在**左右两个已匹配块之间**的证据窗口里找**唯一**同文条目,
    唯一才认。同一短句在窗口内出现两次(「我说:」这类)就放弃 —— 猜错一个页号会
    污染整本页账,而少一条页归属只是"记录缺失",两者代价不对称。
    """
    if not hits:
        return 0
    norms = [normalize(b.text) for b in evidence]
    order = sorted(hits)
    filled = 0
    for pos, block in enumerate(blocks):
        if pos in claimed or block.page_idx is not None:
            continue
        if block.kind not in ("text", "heading"):
            continue
        key = normalize(block.text)
        if not (_SHORT_MIN_CHARS <= len(key) < MIN_PREFIX_CHARS):
            continue
        before = [p for p in order if p < pos]
        after = [p for p in order if p > pos]
        lo = hits[before[-1]] + 1 if before else 0
        hi = hits[after[0]] if after else len(evidence)
        found = [j for j in range(lo, hi) if norms[j] == key]
        if len(found) != 1:
            continue
        ev = evidence[found[0]]
        if ev.page_idx is None:
            continue
        blocks[pos] = block.with_page(ev.page_idx, "interval")
        claimed[pos] = ev.page_idx
        links.append(Link(block_id=block.id, evidence_id=ev.id,
                          confidence="derived", how="window"))
        filled += 1
    return filled


def _fill_intervals(blocks: list[Block], claimed: dict[int, int]) -> int:
    """两侧同页的夹心块继承页号(阅读顺序不变 → 这些块必然同页)。"""
    if not claimed:
        return 0
    order = sorted(claimed)
    filled = 0
    for pos in range(len(blocks)):
        if pos in claimed or blocks[pos].page_idx is not None:
            continue
        before = [p for p in order if p < pos]
        after = [p for p in order if p > pos]
        if not before or not after:
            continue
        left, right = before[-1], after[0]
        if claimed[left] != claimed[right]:
            continue                      # 跨页夹心:真的分不清在页界哪一侧
        blocks[pos] = blocks[pos].with_page(claimed[left], "interval")
        filled += 1
    return filled


def links_monotonic(links: Sequence[Link], blocks: Sequence[Block]) -> bool:
    """``(block.order, evidence.order)`` 是否单调不减(§E.5 不变量 4)。

    对齐算法本身保证单调(游标只前进);这个函数是给测试与将来换算法时的守门人。
    """
    order = {b.id: b.order for b in blocks}
    last_ev = -1
    for link in links:
        ev = order.get(link.evidence_id)
        if ev is None:
            return False
        if ev < last_ev:
            return False
        last_ev = ev
    return True
