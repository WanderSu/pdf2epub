"""``clean-report.verification.pages`` 的契约(P5)。

P4 时这个字典里的 ``blank`` 恒为空,注释写着"判定要渲染 PDF,这里留空"——
一个**看着有效、实际没有语义**的字段。P5 把它改成真实的证据层判定:

    expected / covered        与 ``ir.pages`` 同口径(1-based)
    blank                     证据层里这一页什么内容都没有(只有页码/页眉页脚)
    missing                   全部零归属页(= 后三项 + undecided 的并集)
    unattributed              有正文证据却没块归属 —— 真缺口
    no_text                   有区域、无文字 —— 判不了,要渲染才能定
    undecided                 没有证据层,无从判定

三个新键是**加法**(读端可忽略旧键;旧读端也不会因为多键而读错)。这里守四件事:

1. ``blank`` 不再恒空,而且只收"证据层也空"的页;
2. ``no_text`` 不会被算进 ``blank``(判不了 ≠ 空白);
3. ``missing`` 仍是零归属页的**全量**(不因为拆成三类而缩水);
4. 撤销后的报告用的是撤销后 IR 的账,不是把应用时的数字搬过去。
"""
from __future__ import annotations

from clean import apply_patches, revert_patches
from clean.report import Verification
from ir import build_ir

MD = (
    "# 第一章 起头\n"
    "\n"
    "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线前沿。\n"
)

EVIDENCE = [
    {"type": "text", "text": "第一章 起头", "page_idx": 0},
    {"type": "text",
     "text": "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线前沿。",
     "page_idx": 1},
]


def _pages(evidence=None, *, total_pages: int = 3) -> dict:
    ir = build_ir(MD, backend="mineru", evidence=evidence, total_pages=total_pages)
    result = apply_patches(ir, [], started_at=0, finished_at=0)
    return result.report.verification.pages


def test_pages_keys_are_frozen_in_order() -> None:
    assert list(_pages(EVIDENCE)) == [
        "expected", "covered", "blank", "missing",
        "unattributed", "no_text", "undecided",
    ]


def test_blank_is_a_real_computation_not_a_placeholder() -> None:
    """第 3 页在证据层里没有任何条目 → blank(不是"暂时算不出来")。"""
    pages = _pages(EVIDENCE)
    assert pages["blank"] == [3]
    assert pages["missing"] == [3]
    assert pages["unattributed"] == [] and pages["no_text"] == []


def test_page_with_an_empty_region_goes_to_no_text_not_blank() -> None:
    evidence = EVIDENCE + [
        {"type": "text", "text": "", "page_idx": 2, "bbox": [149, 76, 841, 398]},
        {"type": "page_number", "text": "3", "page_idx": 2},
    ]
    pages = _pages(evidence)
    assert pages["no_text"] == [3]
    assert pages["blank"] == []          # 判不了就不许写"空白"
    assert pages["missing"] == [3]       # 全量口径不变


def test_unattributed_is_reported_separately() -> None:
    evidence = EVIDENCE + [{
        "type": "text", "page_idx": 2,
        "text": "这一段正文在 markdown 里没有对应段落,页号只能空着。",
    }]
    pages = _pages(evidence)
    assert pages["unattributed"] == [3]
    assert pages["blank"] == [] and pages["no_text"] == []


def test_without_evidence_layer_pages_are_undecided() -> None:
    pages = _pages(None)
    assert pages["undecided"] == [1, 2, 3]
    assert pages["blank"] == [] and pages["missing"] == [1, 2, 3]


def test_no_total_pages_means_no_gap_accounting() -> None:
    """``total_pages=0``(没告诉 IR 一共几页)时页集合为空,账目也是空的。"""
    pages = _pages(EVIDENCE, total_pages=0)
    assert pages["expected"] == 0
    assert pages["missing"] == [] and pages["blank"] == []


def test_partition_covers_missing_exactly() -> None:
    """blank + unattributed + no_text + undecided == missing(不重不漏)。"""
    evidence = EVIDENCE + [
        {"type": "text", "text": "", "page_idx": 2, "bbox": [1, 2, 3, 4]},
        {"type": "text", "page_idx": 3, "text": "第四页的正文没有对应段落。"},
    ]
    pages = _pages(evidence, total_pages=4)
    parts = (pages["blank"] + pages["unattributed"]
             + pages["no_text"] + pages["undecided"])
    assert sorted(parts) == pages["missing"]
    assert len(parts) == len(set(parts))


def test_revert_report_recomputes_the_account() -> None:
    """撤销后的报告按撤销后的 IR 重算(页归属不因撤销而凭空变化)。"""
    ir = build_ir(MD, backend="mineru", evidence=EVIDENCE, total_pages=3)
    applied = apply_patches(ir, [], started_at=0, finished_at=0)
    undone = revert_patches(applied.ir, applied.report, finished_at=0)
    assert undone.report.verification.pages == applied.report.verification.pages


def test_verification_roundtrips_with_the_new_keys() -> None:
    pages = _pages(EVIDENCE)
    verification = Verification(roundtrip_ok=True, pages=pages)
    assert Verification.from_dict(verification.to_dict()) == verification
