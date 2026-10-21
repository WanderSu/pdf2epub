"""IR 对齐与"对齐失败安全降级"(阶段 B §E.5 不变量 4、7 / P1 验收第 4 条)。

核心主张:**配不上就空着**。这一组测试用构造夹具证明:

  - 顺序一致的证据 → 页归属正确;
  - 证据里缺一段 → 该块 ``page_idx=None``,不猜;
  - 证据顺序冲突 → 相关 link 丢弃、不产生错误页号,且**渲染结果仍逐字节等于输入**;
  - 两侧同页的夹心块才继承页号(跨页夹心一律不继承);
  - 同一段话重复出现 → 游标前进,不会被同一条证据吃掉两次。

夹具全部是合成文本与合成 ``content_list``,不涉及真实书籍内容。
"""
from __future__ import annotations

import pytest

from ir import (IRError, Link, Provenance, align_blocks, build_ir,
                links_monotonic, render_ir, spans_tile)

MD = """\
# 第一章 起头

第一段正文,内容足够长,能被锚点匹配上。

中间这段在证据里没有对应项,应当保持无页归属。

第三段正文,内容同样足够长,能被锚点匹配上。
"""


def _evidence(*rows):
    return [
        {"type": "text", "text": text, "page_idx": page, "bbox": [10, 20, 300, 40]}
        for text, page in rows
    ]


def _ir(evidence, **kwargs):
    kwargs.setdefault("source", "synthetic.md")
    kwargs.setdefault("backend", "mineru")
    kwargs.setdefault("total_pages", 4)
    return build_ir(MD, evidence=evidence, **kwargs)


def _pages(ir) -> list[int | None]:
    """按块序返回页归属(与 MD 的结构一一对应:标题、三段正文)。"""
    return [b.page_idx for b in ir.editable_blocks]


# ---------------------------------------------------------------- 正常对齐

def test_matching_anchor_assigns_page_and_link() -> None:
    ir = _ir(_evidence(
        ("第一章 起头", 0),
        ("第一段正文,内容足够长,能被锚点匹配上。", 0),
        ("中间这段在证据里没有对应项,应当保持无页归属。", 1),
        ("第三段正文,内容同样足够长,能被锚点匹配上。", 2),
    ))
    assert _pages(ir) == [0, 0, 1, 2]
    assert len(ir.links) == 4
    assert all(link.confidence == "derived" for link in ir.links)
    assert links_monotonic(ir.links, ir.blocks)
    assert render_ir(ir) == MD
    assert spans_tile(ir, MD)[0]


def test_missing_evidence_leaves_page_empty_instead_of_guessing() -> None:
    """证据里没有中间那段 → 保持 None;两侧页号不同 → 不做区间继承。"""
    ir = _ir(_evidence(
        ("第一章 起头", 0),
        ("第一段正文,内容足够长,能被锚点匹配上。", 0),
        ("第三段正文,内容同样足够长,能被锚点匹配上。", 2),
    ))
    pages = _pages(ir)
    assert pages == [0, 0, None, 2]             # 跨页夹心:不猜
    assert ir.counts()["page_attribution_missing"] == 1
    assert ir.meta.counts["unmatched"] >= 1
    assert render_ir(ir) == MD


def test_interval_inheritance_requires_same_page_on_both_sides() -> None:
    """两侧同页 → 夹心块继承(阅读顺序不变,它们必然同页)。"""
    ir = _ir(_evidence(
        ("第一章 起头", 3),
        ("第一段正文,内容足够长,能被锚点匹配上。", 3),
        ("第三段正文,内容同样足够长,能被锚点匹配上。", 3),
    ))
    pages = _pages(ir)
    assert pages == [3, 3, 3, 3]
    assert ir.counts()["page_attribution_missing"] == 0
    # 继承来的页号标 interval,不能冒充后端证据
    middle = next(b for b in ir.editable_blocks if "中间这段" in b.text)
    assert middle.prov.page_idx_source == "interval"


def test_repeated_paragraph_consumes_distinct_evidence() -> None:
    """同一段话出现两次 → 两条证据各配一次,不会两个块抢同一条。"""
    md = "重复的段落内容,长度足够。\n\n重复的段落内容,长度足够。\n"
    ir = build_ir(md, backend="mineru", total_pages=3, evidence=_evidence(
        ("重复的段落内容,长度足够。", 0),
        ("重复的段落内容,长度足够。", 2),
    ))
    assert [b.page_idx for b in ir.editable_blocks] == [0, 2]
    assert render_ir(ir) == md


# ---------------------------------------------------------------- 安全降级

def test_conflicting_evidence_order_drops_links_and_keeps_text_intact() -> None:
    """证据顺序与 md 相反 → 后续块配不上;不产生错误页号,渲染一字节不差。"""
    ir = _ir(_evidence(
        ("第三段正文,内容同样足够长,能被锚点匹配上。", 2),
        ("第一段正文,内容足够长,能被锚点匹配上。", 0),
    ))
    pages = _pages(ir)
    # 第一段仍能找到自己的证据(游标跳过不匹配的项),第三段则落在游标之前 → 丢弃
    assert pages[1] == 0
    assert pages[3] is None
    assert ir.meta.counts["unmatched"] >= 1
    assert links_monotonic(ir.links, ir.blocks)
    assert render_ir(ir) == MD
    assert spans_tile(ir, MD)[0]


def test_evidence_without_page_number_grants_no_page() -> None:
    ir = _ir([
        {"type": "text", "text": "第一段正文,内容足够长,能被锚点匹配上。"},
        {"type": "text", "text": "第三段正文,内容同样足够长,能被锚点匹配上。"},
    ])
    assert all(b.page_idx is None for b in ir.editable_blocks)
    assert ir.links == ()
    assert render_ir(ir) == MD


def test_no_evidence_means_no_claim_at_all() -> None:
    ir = _ir(None)
    assert ir.evidence_blocks == ()
    assert ir.links == ()
    assert all(b.page_idx is None for b in ir.editable_blocks)
    assert ir.counts()["page_attribution_missing"] == len(ir.editable_blocks)
    assert ir.meta.backend_evidence["page_attribution"] == "unavailable"


def test_strict_mode_refuses_partial_attribution() -> None:
    """P1 默认宽松(先观测),P5 才收紧;严格模式下必须当场失败。"""
    with pytest.raises(IRError):
        _ir(_evidence(("第一段正文,内容足够长,能被锚点匹配上。", 0)), strict_pages=True)


def test_strict_mode_passes_when_everything_is_attributed() -> None:
    ir = _ir(_evidence(
        ("第一章 起头", 0),
        ("第一段正文,内容足够长,能被锚点匹配上。", 0),
        ("中间这段在证据里没有对应项,应当保持无页归属。", 0),
        ("第三段正文,内容同样足够长,能被锚点匹配上。", 0),
    ), strict_pages=True)
    assert ir.counts()["page_attribution_missing"] == 0


def test_short_blocks_are_not_anchored() -> None:
    """太短的块没有区分度,不参与匹配(否则"同上"这类词会乱配)。"""
    md = "同上\n\n第一段正文,内容足够长,能被锚点匹配上。\n"
    ir = build_ir(md, backend="mineru", total_pages=2, evidence=_evidence(
        ("同上", 0),
        ("第一段正文,内容足够长,能被锚点匹配上。", 1),
    ))
    assert ir.editable_blocks[0].page_idx is None       # 太短,不配对
    assert ir.evidence_blocks[0].page_idx == 0          # 证据本身仍有页号


# ---------------------------------------------------------------- 证据类型映射

def test_mineru_text_level_becomes_heading() -> None:
    ir = _ir([{"type": "text", "text": "起头", "text_level": 2, "page_idx": 0}])
    ev = ir.evidence_blocks[0]
    assert ev.kind == "heading" and ev.level == 2
    assert ev.kind_confidence == "explicit"


def test_mineru_marks_structural_blocks_explicitly() -> None:
    ir = _ir([
        {"type": "page_number", "text": "12", "page_idx": 0},
        {"type": "page_footnote", "text": "脚注", "page_idx": 0},
        {"type": "header", "text": "书名", "page_idx": 0},
        {"type": "ref_text", "text": "参考文献", "page_idx": 0},
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0},
    ])
    kinds = [b.kind for b in ir.evidence_blocks]
    assert kinds == ["page_number", "footnote", "header", "reference", "image"]
    assert all(b.kind_confidence == "explicit" for b in ir.evidence_blocks)


def test_paddle_labels_map_to_heading_with_derived_level() -> None:
    ir = build_ir(MD, backend="paddleocr", total_pages=1, evidence=[
        {"type": "text", "label": "doc_title", "text": "书名", "page_idx": 0,
         "bbox": [100, 50, 900, 90]},
        {"type": "text", "label": "paragraph_title", "text": "一 前言", "page_idx": 0,
         "bbox": [100, 200, 400, 230]},
        {"type": "text", "label": "number", "text": "3", "page_idx": 0},
    ])
    titles = [b for b in ir.evidence_blocks if b.kind == "heading"]
    assert [t.level for t in titles] == [1, None]
    assert all(t.kind_confidence == "derived" for t in titles)
    assert ir.evidence_blocks[-1].kind == "page_number"


def test_page_break_blocks_are_not_content_evidence() -> None:
    ir = build_ir(MD, backend="paddleocr", total_pages=1, evidence=[
        {"type": "page_break", "page_idx": 0, "offset": 0},
        {"type": "text", "text": "第一段正文,内容足够长,能被锚点匹配上。", "page_idx": 0},
    ])
    assert all(b.prov.raw_type != "page_break" for b in ir.evidence_blocks)


def test_page_offset_shifts_evidence_pages() -> None:
    """分片时证据页号是段内 0-based,必须加上段偏移才是整本页序。"""
    ir = _ir(_evidence(("第一段正文,内容足够长,能被锚点匹配上。", 0)),
             page_offset=200, total_pages=400)
    assert ir.evidence_blocks[0].page_idx == 200
    first = ir.editable_blocks[1]
    assert first.page_idx == 200
    assert first.prov.page_idx_source == "backend"


# ---------------------------------------------------------------- 坐标与不变量

def test_bbox_without_coordinate_space_is_dropped() -> None:
    prov = Provenance(backend="mineru", part_id="1-1", block_index=0,
                      bbox=(1, 2, 3, 4), bbox_space="")
    assert prov.bbox is None and prov.bbox_space == ""


def test_unknown_coordinate_space_is_refused() -> None:
    with pytest.raises(IRError):
        Provenance(backend="mineru", part_id="1-1", block_index=0,
                   bbox=(1, 2, 3, 4), bbox_space="pixels-ish")


def test_page_without_source_is_refused() -> None:
    with pytest.raises(IRError):
        Provenance(backend="mineru", part_id="1-1", block_index=0, page_idx=3)


def test_links_monotonic_rejects_backwards_links() -> None:
    ir = _ir(_evidence(
        ("第一段正文,内容足够长,能被锚点匹配上。", 0),
        ("第三段正文,内容同样足够长,能被锚点匹配上。", 1),
    ))
    first, second = ir.links[0], ir.links[1]
    backwards = (Link(second.block_id, second.evidence_id, "derived"),
                 Link(first.block_id, first.evidence_id, "derived"))
    assert links_monotonic(ir.links, ir.blocks) is True
    assert links_monotonic(backwards, ir.blocks) is False


def test_alignment_does_not_mutate_input_blocks() -> None:
    """对齐返回新块,原块保持 page_idx=None(纯函数,方便重跑与差分)。"""
    ir = _ir(_evidence(("第一段正文,内容足够长,能被锚点匹配上。", 0)))
    from ir import parse_markdown

    original = parse_markdown(MD).blocks          # 未对齐的原始块
    evidence = ir.evidence_blocks
    aligned, links, stats = align_blocks(original, evidence)
    assert all(b.page_idx is None for b in original)
    assert any(b.page_idx is not None for b in aligned)
    assert links and stats["anchor_hits"] >= 1
