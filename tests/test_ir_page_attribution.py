"""页归属审计(P4):只统计与报问题,不补页号。

这一组守四条:

1. **不补页号**:配不上的块 ``page_idx`` 保持 ``None``,审计只把它数出来;
2. **硬错误只留给数据矛盾**(页号越界 / 页序倒退),缺页是"可接受的缺口";
3. **按 producer 与块类型分组**,并且问题**可定位**(块 id + 原文片段);
4. **无证据层**时缺页是输入形态决定的,报 ``info`` 而不是失败;
5. **零归属页按证据层分成四类**(P5):blank / no_text / unattributed /
   unavailable —— 判不了的必须写成判不了,不能拿"空白页"盖过去。
"""
from __future__ import annotations

import pytest

from ir import (IRError, Producer, audit_page_attribution, build_ir,
                covered_pages, format_attribution, gaps_by_state,
                page_coverage, uncovered_pages)
from ir.model import Block, Provenance
from ir.pages import PageAttribution, PageAuditError, PageGap, PageIssue

MD = (
    "# 第一章 起头\n"
    "\n"
    "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线前沿。\n"
    "\n"
    "这是第二段正文,同样以句号结尾。\n"
)

#: 与 MD 的三段一一对应的证据(页号 0、1、1)
EVIDENCE = [
    {"type": "text", "text": "第一章 起头", "page_idx": 0},
    {"type": "text",
     "text": "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线前沿。",
     "page_idx": 1},
    {"type": "text", "text": "这是第二段正文,同样以句号结尾。", "page_idx": 1},
]


def _build(md: str = MD, **kwargs):
    kwargs.setdefault("backend", "mineru")
    kwargs.setdefault("total_pages", 3)
    return build_ir(md, **kwargs)


# ---------------------------------------------------------------- 三种形态

def test_without_evidence_layer_missing_is_expected_not_failure() -> None:
    """没有证据层:缺页是输入形态,不是对齐失败 —— info 且不判错。"""
    report = audit_page_attribution(_build())
    assert report.evidence_state == "unavailable"
    assert report.required == 3
    assert report.attributed == 0
    assert report.missing == 3
    assert report.coverage == 0.0
    assert report.ok                                   # 缺页不是硬错误
    assert not report.errors
    assert all(i.severity == "info" for i in report.issues
               if i.code == "missing_attribution")
    assert all("没有证据层" in i.detail for i in report.issues
               if i.code == "missing_attribution")


def test_full_alignment_reports_complete_coverage() -> None:
    report = audit_page_attribution(_build(evidence=EVIDENCE))
    assert report.evidence_state == "complete"
    assert report.attributed == 3
    assert report.missing == 0
    assert report.coverage == 1.0
    assert report.ok


def test_partial_alignment_is_a_locatable_warning() -> None:
    """有证据层却没配上:warning + 块 id + 原文片段(可人工复核)。"""
    evidence = [EVIDENCE[0], EVIDENCE[1]]              # 第三段没有对应证据
    report = audit_page_attribution(_build(evidence=evidence))
    assert report.evidence_state == "partial"
    assert report.missing == 1
    assert report.ok                                   # 可接受,不是硬错误
    missing = [i for i in report.issues if i.code == "missing_attribution"]
    assert len(missing) == 1
    assert missing[0].severity == "warning"
    assert missing[0].block_id == "book#m2"
    assert missing[0].kind == "text"
    assert missing[0].snippet.startswith("这是第二段正文")


def test_coverage_is_none_when_nothing_is_required() -> None:
    """没有需要归属的块时是 None,不是 0.0(免得读成"归属率 0%")。"""
    report = audit_page_attribution(build_ir("", backend="mineru"))
    assert report.required == 0
    assert report.coverage is None
    assert page_coverage(build_ir("", backend="mineru")) is None


# ---------------------------------------------------------------- 分组维度

def test_grouping_by_kind_and_producer() -> None:
    producer = Producer.from_backend("mineru", source="s.pdf")
    report = audit_page_attribution(
        _build(evidence=EVIDENCE, producer=producer))
    assert report.producer == "mineru"
    assert report.producer_known is True
    assert report.by_kind["heading"] == {"required": 1, "attributed": 1, "missing": 0}
    assert report.by_kind["text"]["required"] == 2
    assert "backend" in report.by_source


def test_unknown_producer_is_a_warning_not_a_hard_error() -> None:
    """``build_ir`` 不显式给 producer 时记 unknown —— 那是**合规调用**,不是失败。"""
    report = audit_page_attribution(_build(evidence=EVIDENCE))
    assert report.producer == "unknown"
    assert report.producer_known is False
    assert report.ok
    codes = {i.code for i in report.issues}
    assert "producer_unknown" in codes
    assert all(i.severity == "warning" for i in report.issues
               if i.code == "producer_unknown")


def test_page_markers_and_rules_are_exempt() -> None:
    """页码注释自带 ``extra['page']``、分隔线不承载内容:不计入 required。"""
    md = ("这是第一段用于测试的正文内容,长度足够对齐。\n"
          "\n<!-- page 2 -->\n"
          "\n---\n"
          "\n这是第二段用于测试的正文内容,长度足够对齐。\n")
    ir = _build(md, evidence=[
        {"type": "text", "text": "这是第一段用于测试的正文内容,长度足够对齐。",
         "page_idx": 0},
        {"type": "text", "text": "这是第二段用于测试的正文内容,长度足够对齐。",
         "page_idx": 1},
    ])
    report = audit_page_attribution(ir)
    assert report.total_editable == 4
    assert report.required == 2
    assert report.exempt == 2
    assert report.missing == 0


# ---------------------------------------------------------------- 硬错误

def test_page_beyond_total_pages_is_a_hard_error() -> None:
    ir = _build(evidence=EVIDENCE, total_pages=2)      # 页号 1(第 2 页)仍合法
    assert audit_page_attribution(ir).ok
    tight = _build(evidence=EVIDENCE, total_pages=1)   # 只剩第 1 页 → 越界
    report = audit_page_attribution(tight)
    assert not report.ok
    assert [i.code for i in report.errors] == ["page_out_of_range", "page_out_of_range"]
    assert "超出 1..1" in report.errors[0].detail
    with pytest.raises(PageAuditError):
        report.raise_for_errors()


def test_page_regression_is_a_hard_error() -> None:
    """页序倒退 = md 段序与证据块序已经不一致,任何归属都不可信。"""
    md = "第一段内容足够长且以句号结尾。\n\n第二段内容同样以句号结尾。\n"
    ir = _build(md, evidence=[
        {"type": "text", "text": "第一段内容足够长且以句号结尾。", "page_idx": 2},
        {"type": "text", "text": "第二段内容同样以句号结尾。", "page_idx": 0},
    ])
    report = audit_page_attribution(ir)
    assert not report.ok
    codes = [i.code for i in report.errors]
    assert "page_regression" in codes
    issue = next(i for i in report.errors if i.code == "page_regression")
    assert issue.block_id == "book#m1"
    assert "早于前面已归属的 3 页" in issue.detail


def test_a_block_with_a_page_but_no_source_cannot_be_built() -> None:
    """``page_idx`` 有值却说不出来源 —— 模型层直接拒绝(审计里的兜底不可达)。"""
    with pytest.raises(IRError):
        Provenance(backend="mineru", part_id="book", block_index=0,
                   page_idx=3, page_idx_source="none")
    block = Block(id="book#m0", kind="text", text="正文。", order=0,
                  prov=Provenance(backend="mineru", part_id="book", block_index=0),
                  kind_confidence="explicit", editable=True, md_span=(0, 3))
    with pytest.raises(IRError):
        block.with_page(3, "none")


def test_uncovered_pages_are_informational_only() -> None:
    """零归属页不是硬错误;它的**成因**由 ``gaps`` 按证据层给出。"""
    report = audit_page_attribution(_build(evidence=EVIDENCE))
    # page_idx 是 0-based、ir.pages 是 1-based:第 1、2 页有内容 → 缺第 3 页
    assert report.uncovered_pages == (3,)
    issue = next(i for i in report.issues if i.code == "uncovered_page")
    assert issue.severity == "info"
    assert report.ok


def test_uncovered_pages_use_the_one_based_page_numbering() -> None:
    """页号口径:``page_idx`` 0-based 与 ``ir.pages`` 1-based 之间的换算只做一次。

    曾经在这两处直接做集合差,结果整套页号错位一格 —— 明明每页都有内容,
    却报「全部页未覆盖」。
    """
    ir = _build(evidence=EVIDENCE, total_pages=3)
    assert covered_pages(ir) == (1, 2)
    assert uncovered_pages(ir) == (3,)
    # 全部覆盖时一个都不缺
    full = _build(evidence=[EVIDENCE[0], EVIDENCE[1],
                            dict(EVIDENCE[2], page_idx=2)], total_pages=3)
    assert uncovered_pages(full) == ()


# ---------------------------------------------------------------- 序列化与展示

def test_report_dict_roundtrips() -> None:
    report = audit_page_attribution(_build(evidence=EVIDENCE))
    assert PageAttribution.from_dict(report.to_dict()) == report


def test_issue_dict_roundtrips() -> None:
    issue = PageIssue("warning", "missing_attribution", "book#m1", 1, "text",
                      None, "none", "缺页", "片段")
    assert PageIssue.from_dict(issue.to_dict()) == issue


def test_unknown_severity_or_code_is_refused() -> None:
    with pytest.raises(PageAuditError):
        PageIssue("critical", "missing_attribution")
    with pytest.raises(PageAuditError):
        PageIssue("error", "typo_code")


def test_format_summary_carries_the_numbers() -> None:
    text = format_attribution(audit_page_attribution(_build(evidence=EVIDENCE)))
    assert "producer=unknown" in text
    assert "已归属 3" in text
    assert "缺失 0" in text


# ---------------------------------------------------------------- 零归属成因(P5)
#
# 四类成因的处置完全不同,混成一句"可能是空白页"等于把该查的那一页也放过:
#   blank        证据层里什么都没留下(只有页码/页眉页脚或干脆没条目)→ 接受
#   no_text      有版面区域、没有可识别文字 → 判不了(要渲染 PDF),不冒充 blank
#   unattributed 有正文却没块归属 → 真缺口
#   unavailable  没有证据层 → 无从判定

def test_page_without_any_evidence_entry_is_blank() -> None:
    report = audit_page_attribution(_build(evidence=EVIDENCE))
    gap = report.gaps[0]
    assert (gap.page, gap.state) == (3, "blank")
    assert gap.evidence == 0 and gap.content == 0
    assert report.gap_pages("blank") == (3,)
    assert report.gap_pages("no_text") == ()


def test_page_with_an_empty_text_region_is_not_called_blank() -> None:
    """有区域、无文字(扫描图版/插图页):只能判"判不了",不能签"空白页"的字。"""
    evidence = EVIDENCE + [
        {"type": "text", "text": "", "page_idx": 2, "bbox": [149, 76, 841, 398]},
        {"type": "page_number", "text": "3", "page_idx": 2},
    ]
    report = audit_page_attribution(_build(evidence=evidence))
    gap = report.gaps[0]
    assert (gap.page, gap.state) == (3, "no_text")
    assert gap.evidence == 2          # 空文本区域 + 页码
    assert gap.content == 0           # 都不承载正文
    assert gap.regions == 1           # 一个"有区域无文字"
    assert report.gap_pages("blank") == ()


def test_page_number_alone_does_not_make_a_page_contentful() -> None:
    evidence = EVIDENCE + [{"type": "page_number", "text": "3", "page_idx": 2}]
    gap = audit_page_attribution(_build(evidence=evidence)).gaps[0]
    assert gap.state == "blank"       # 页码只证明这页被扫过
    assert gap.evidence == 1 and gap.content == 0 and gap.regions == 0


def test_page_with_content_evidence_but_no_block_is_unattributed() -> None:
    """有非空正文证据却没块归属 —— 这才是真正的对齐缺口,报 warning。"""
    evidence = EVIDENCE + [{
        "type": "text", "page_idx": 2,
        "text": "这一段正文在 markdown 里根本没有对应段落,所以页号只能空着。",
    }]
    report = audit_page_attribution(_build(evidence=evidence))
    gap = report.gaps[0]
    assert (gap.page, gap.state) == (3, "unattributed")
    assert gap.content == 1
    issue = next(i for i in report.issues if i.code == "uncovered_page")
    assert issue.severity == "warning"
    assert "unattributed 1" in issue.detail
    assert report.ok                 # 仍是"可查"而非"硬错误"


def test_without_evidence_layer_every_gap_is_undecidable() -> None:
    report = audit_page_attribution(_build())
    assert report.gap_pages("unavailable") == (1, 2, 3)
    assert report.gap_pages("blank") == ()
    assert all(g.detail.startswith("没有证据层") for g in report.gaps)


def test_gap_dict_roundtrips() -> None:
    report = audit_page_attribution(_build(evidence=EVIDENCE))
    assert PageAttribution.from_dict(report.to_dict()) == report
    assert PageGap.from_dict(report.gaps[0].to_dict()) == report.gaps[0]


def test_unknown_gap_state_is_refused() -> None:
    with pytest.raises(PageAuditError):
        PageGap(page=1, state="maybe")
    with pytest.raises(PageAuditError):
        gaps_by_state(_build(evidence=EVIDENCE), "maybe")


def test_audit_does_not_mutate_the_ir() -> None:
    """审计是只读的:跑完 IR 与渲染结果都不变(它绝不能"顺手补个页号")。"""
    ir = _build()
    before = [b.page_idx for b in sorted(ir.editable_blocks, key=lambda b: b.order)]
    audit_page_attribution(ir)
    after = [b.page_idx for b in sorted(ir.editable_blocks, key=lambda b: b.order)]
    assert before == after == [None, None, None]
