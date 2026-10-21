"""连续空行压缩迁移到 Patch 体系后的行为测试(P4)。

被删掉的旧清理器(P6)里 ``clean_markdown`` 的第 5 步与第 9 步是同一个**无条件**表达式:
``re.sub(r"\\n{3,}", "\\n\\n", md)``。它不属于九个开关,所以迁移后也不是启发式
(``confidence="derived"``),但必须同样可审计、可撤销。

覆盖面(只测这条规则实际适用的边界):
  - 正常压缩:≥3 连续换行 → 2;单个空行、行尾符一律不动;
  - 不越界:代码块内部的空行、脚注内部、表格、诗行、标题都**一个字符不改**
    (legacy 靠掩码保护,IR 版靠"只改块间空白"这个作用域);
  - 段间分隔:压缩后仍保留 1 个空行,不会把两段粘成一段;
  - 幂等与撤销:第二次运行零提案;apply → revert 逐字节还原;
  - legacy 对拍:合成输入与真实形态输入上新旧逐字节一致;
  - 文首空白(P5):走文档级 ``set_prefix``,结果与 legacy 的"整段丢"一致;
  - 注释内部空行:legacy 会压、IR 保留 —— **已知差异**,在 notes 里报出来。

夹具全部合成,不含真实书籍正文。
"""
from __future__ import annotations

import difflib
import re

import pytest

from clean import (apply_patches, compress_blank_runs, propose_blank_lines,
                   propose_rules, revert_patches)
from clean.rules import PIPELINE_ORDER
from ir import build_ir, render_ir

MD = "第一段正文内容以句号结尾。\n\n\n\n第二段正文内容同样以句号结尾。\n"


def _ir(md: str = MD, *, evidence: list | None = None, **kwargs):
    return build_ir(md, source="synthetic.md", backend="synthetic",
                    evidence=evidence, **kwargs)


def _run(md: str = MD, *, evidence: list | None = None, allow_apply: bool = True):
    ir = _ir(md, evidence=evidence)
    proposal = propose_blank_lines(ir)
    if not proposal.patches:
        return md, []
    result = apply_patches(ir, proposal.patches, allow_heuristic_apply=allow_apply,
                           started_at=0, finished_at=0)
    return render_ir(result.ir), [result.report]


def _legacy(md: str) -> str:
    """同一输入、所有清理项都关掉的 legacy 清理器(对照组)。

    空行压缩在 legacy 里是**无条件**步骤,关掉九个开关它照样跑 —— 这正是它
    不该被建模成"启发式规则"的原因。
    """
    from legacy_snapshot import frozen

    return frozen("clean_none", md)


def _legacy_join(md: str) -> str:
    """legacy **带头拼接**的产物(生产管线的形态)。

    文首空白与文件末尾换行都是 ``_join_broken_lines`` 重建时顺带丢掉的;关掉
    ``join_lines`` 时它们反而都还在 —— 所以对拍文首空白必须打开它。
    """
    from legacy_snapshot import frozen

    return frozen("join_all", md)


def _assert_matches_legacy(md: str) -> str:
    out, _ = _run(md)
    assert out == _legacy(md), "".join(
        difflib.unified_diff(_legacy(md).splitlines(keepends=True),
                             out.splitlines(keepends=True), "legacy", "ir"))
    return out


# ---------------------------------------------------------------- 正则语义

def test_compress_blank_runs_matches_the_legacy_expression() -> None:
    assert compress_blank_runs("\n\n\n") == "\n\n"
    assert compress_blank_runs("\n\n\n\n\n") == "\n\n"
    assert compress_blank_runs("正文\n\n\n\n正文") == "正文\n\n正文"
    # 边界:不到 3 连换行一律不动(单个空行是段间分隔,不能压掉)
    assert compress_blank_runs("\n") == "\n"
    assert compress_blank_runs("\n\n") == "\n\n"
    # 空行里夹了空格就不是"连续换行",legacy 同样不认
    assert compress_blank_runs("\n \n\n") == "\n \n\n"


# ---------------------------------------------------------------- 正常压缩

def test_three_or_more_newlines_collapse_to_one_blank_line() -> None:
    out, reports = _run()
    assert out == "第一段正文内容以句号结尾。\n\n第二段正文内容同样以句号结尾。\n"
    assert [p.status for p in reports[0].patches] == ["applied"]
    patch = reports[0].patches[0]
    assert patch.rule_id == "blank_lines"
    assert patch.operation == "replace_text"
    assert patch.after is not None and patch.after.text == patch.before.text
    assert patch.after.trailer == "\n\n"
    assert patch.before.trailer == "\n\n\n\n"


def test_single_blank_lines_and_paragraph_breaks_are_left_alone() -> None:
    md = "第一段以句号结尾。\n\n第二段以句号结尾。\n\n第三段以句号结尾。\n"
    assert propose_blank_lines(_ir(md)).patches == ()
    assert _run(md)[0] == md


def test_no_trailing_gap_produces_no_patch() -> None:
    assert propose_blank_lines(_ir("只有一段正文。")).patches == ()


def test_rule_is_not_heuristic_and_applies_by_default() -> None:
    """空行压缩是确定性归一化(数换行),不是猜测 —— 默认运行就该应用。"""
    ir = _ir()
    proposal = propose_blank_lines(ir)
    assert proposal.patches
    assert all(p.confidence == "derived" for p in proposal.patches)
    # 不打开 allow_heuristic_apply 也照样执行(与 legacy 的无条件步骤一致)
    result = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    assert all(p.status == "applied" for p in result.patches)
    assert result.md_after == "第一段正文内容以句号结尾。\n\n第二段正文内容同样以句号结尾。\n"


# ---------------------------------------------------------------- 不越界

def test_code_fence_interior_blank_lines_are_untouched() -> None:
    """代码围栏内部是**内容**:legacy 靠掩码保护,IR 版根本不进这个作用域。"""
    md = ("```python\n"
          "x = 1\n"
          "\n"
          "\n"
          "y = 2\n"
          "```\n"
          "\n"
          "\n"
          "\n"
          "代码之后的正文段落。\n")
    out = _assert_matches_legacy(md)
    assert "x = 1\n\n\ny = 2" in out          # 围栏内部原样
    assert out.endswith("```\n\n代码之后的正文段落。\n")   # 围栏之后的空行照压


def test_footnote_and_table_blocks_keep_their_interior() -> None:
    md = ("[^1]: 列宁:《告犹太工人书》,人民出版社版。\n"
          "    续行缩进四格属于脚注内容。\n"
          "\n"
          "\n"
          "\n"
          "| 甲 | 乙 |\n"
          "| -- | -- |\n"
          "\n"
          "\n"
          "\n"
          "表格之后的正文段落以句号结尾。\n")
    out = _assert_matches_legacy(md)
    assert "    续行缩进四格属于脚注内容。" in out
    assert "| 甲 | 乙 |\n| -- | -- |" in out


def test_verse_lines_and_headings_are_not_collapsed() -> None:
    md = ("# 第一章 标题\n"
          "\n"
          "\n"
          "\n"
          "白日依山尽,\n"
          "黄河入海流。\n"
          "\n"
          "\n"
          "\n"
          "正文段落以句号结尾。\n")
    out = _assert_matches_legacy(md)
    assert "白日依山尽,\n黄河入海流。" in out


def test_page_marker_keeps_its_own_line_and_its_page() -> None:
    """页码注释是页界证据:压缩它后面的空行动不了它本身。"""
    md = ("正文段落以句号结尾。\n"
          "\n"
          "\n"
          "\n"
          "<!-- page 2 -->\n"
          "\n"
          "\n"
          "\n"
          "下一页的正文段落以句号结尾。\n")
    out = _assert_matches_legacy(md)
    assert "<!-- page 2 -->" in out
    ir_after = _ir(out)
    marker = next(b for b in ir_after.editable_blocks
                  if b.extra.get("role") == "page_marker")
    assert marker.extra["page"] == 2


def test_paragraph_separator_survives_compression() -> None:
    """压缩 ≥2 空行 → 1 空行,绝不会把两段粘成一段。"""
    md = "甲段以句号结尾。\n\n\n\n\n\n\n乙段以句号结尾。\n"
    out, _ = _run(md)
    assert out == "甲段以句号结尾。\n\n乙段以句号结尾。\n"
    assert "\n\n" in out


# ---------------------------------------------------------------- 幂等与撤销

def test_second_run_produces_nothing() -> None:
    once, _ = _run()
    assert propose_blank_lines(_ir(once)).patches == ()


def test_replaying_the_report_on_its_own_input_is_idempotent() -> None:
    """同一份输入上重放:结果逐字节相同(报告与输入指纹绑定)。"""
    ir = _ir()
    proposal = propose_blank_lines(ir)
    first = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    replay = apply_patches(ir, first.report.patches, started_at=0, finished_at=0)
    assert replay.md_after == first.md_after


def test_replaying_the_report_after_the_change_is_refused() -> None:
    """改完之后重放:输入指纹不符 → 全批 source_mismatch,一个字节不动。"""
    ir = _ir()
    proposal = propose_blank_lines(ir)
    first = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    replay = apply_patches(first.ir, first.report.patches,
                           started_at=0, finished_at=0)
    assert all(p.status == "failed" for p in replay.patches)
    assert all(p.skip_reason == "source_mismatch" for p in replay.patches)
    assert replay.md_after == first.md_after


def test_replaying_the_report_on_the_same_input_then_reverting_is_stable() -> None:
    """重放后撤销仍回到原文(撤销只认 after.sha256,不认重放了几次)。"""
    ir = _ir()
    proposal = propose_blank_lines(ir)
    first = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    replay = apply_patches(ir, first.report.patches, started_at=0, finished_at=0)
    undone = revert_patches(replay.ir, replay.report, finished_at=0)
    assert undone.md_after == MD


def test_apply_then_revert_restores_original_bytes() -> None:
    ir = _ir()
    proposal = propose_blank_lines(ir)
    result = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    undone = revert_patches(result.ir, result.report, finished_at=0)
    assert undone.md_after == MD
    assert all(p.status == "applied" for p in undone.patches)


def test_revert_refuses_when_the_gap_was_edited_by_hand() -> None:
    ir = _ir()
    result = apply_patches(ir, propose_blank_lines(ir).patches,
                           started_at=0, finished_at=0)
    target = result.report.patches[0].target_block_id
    block = result.ir.block(target)
    assert block is not None
    from dataclasses import replace as _replace

    tampered = result.ir.replace_block(
        block.id, _replace(block, extra={**block.extra, "trailer": "\n\n\n\n\n"}))
    undone = revert_patches(tampered, result.report, finished_at=0)
    assert all(p.status == "failed" for p in undone.patches)
    assert all(p.skip_reason == "after_mismatch" for p in undone.patches)
    assert render_ir(undone.ir) == render_ir(tampered)      # 一个字节都没动


def test_source_fingerprint_mismatch_refuses_the_whole_batch() -> None:
    """报告属于另一次输入时,空行压缩同样一步不走(Patch 引擎的通用闸门)。"""
    ir = _ir()
    other = _ir("完全不同的另一份输入正文以句号结尾。\n")
    proposal = propose_blank_lines(other)
    result = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    assert all(p.status == "failed" for p in result.patches)
    assert all(p.skip_reason == "source_mismatch" for p in result.patches)
    assert result.md_after == render_ir(ir)


# ---------------------------------------------------------------- legacy 对拍

def test_matches_legacy_byte_for_byte_on_shaped_input() -> None:
    _assert_matches_legacy(MD)


def test_matches_legacy_on_a_document_shaped_like_a_real_book() -> None:
    md = ("# 第一章 起头\n"
          "\n"
          "\n"
          "\n"
          "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线前沿。\n"
          "\n"
          "![](images/aaaa.jpg)\n"
          "\n"
          "\n"
          "\n"
          "```\n"
          "a = 1\n"
          "\n"
          "b = 2\n"
          "```\n"
          "\n"
          "\n"
          "\n"
          "[^1]: 列宁:《告犹太工人书》,人民出版社版。\n"
          "\n"
          "\n\n"
          "正文最后一段以句号结尾。\n")
    _assert_matches_legacy(md)


def test_pipeline_leaves_no_residual_blank_runs() -> None:
    """四条规则跑完,产物里不应再有 3+ 连续换行(legacy 第 9 步的效果)。"""
    ir = _ir(md=("第一段正文内容以句号结尾。\n"
                 "\n\n\n\n"
                 "第二段正文内容同样以句号结尾。\n"
                 "\n\n\n"
                 "第三段正文内容同样以句号结尾。\n"))
    current, reports = propose_rules(ir, PIPELINE_ORDER, allow_apply=True)
    out = render_ir(current)
    assert not re.search(r"\n{3,}", out)
    assert "blank_lines" in {p.rule_id for r in reports for p in r.patches}


# ---------------------------------------------------------------- 文首空白(P5)

def test_document_prefix_is_dropped_through_a_document_level_patch() -> None:
    """文首空白现在走 ``set_prefix``:legacy 的拼接重建本来就会把它整段丢掉。

    P4 时这一步没有文档级 operation,只能报 note;P5 补上之后它是一条真实的、
    可撤销的 Patch —— 而且和 legacy 走到同一个结果。
    """
    md = "\n\n\n\n第一段正文以句号结尾。\n"
    proposal = propose_blank_lines(_ir(md))
    assert len(proposal.patches) == 1
    patch = proposal.patches[0]
    assert patch.operation == "set_prefix"
    assert patch.target_block_id == "book@prefix"
    assert patch.before.text == "\n\n\n\n"
    assert patch.after is not None and patch.after.text == ""
    assert patch.confidence == "derived"
    out, _ = _run(md)
    assert out == "第一段正文以句号结尾。\n"


def test_single_leading_blank_line_is_also_dropped() -> None:
    """legacy 不是"压到 ≤2 行",是**整段丢**:一个空行也不留。

    对拍用 ``_legacy_join``:丢文首空白这件事发生在拼接重建里,关掉拼接它就还在。
    ``+ "\\n"`` 是 P3 已记录的显式分歧 —— legacy 连文件末尾换行一起丢,IR 保留。
    """
    for md in ("\n正文一段以句号结尾。\n", "  \n正文一段以句号结尾。\n"):
        out, _ = _run(md)
        assert out == "正文一段以句号结尾。\n"
        assert _legacy_join(md) + "\n" == out


def test_prefix_patch_apply_revert_is_byte_identical() -> None:
    md = "\n\n\n正文一段以句号结尾。\n\n正文二段以句号结尾。\n"
    ir = _ir(md)
    result = apply_patches(ir, propose_blank_lines(ir).patches,
                           started_at=0, finished_at=0)
    assert result.ir.meta.prefix == ""
    undone = revert_patches(result.ir, result.report, finished_at=0)
    assert undone.ir.meta.prefix == "\n\n\n"
    assert undone.md_after == md


def test_prefix_patch_is_idempotent() -> None:
    md = "\n\n\n正文一段以句号结尾。\n"
    once, _ = _run(md)
    assert propose_blank_lines(_ir(once)).patches == ()


def test_prefix_patch_refuses_when_the_prefix_was_changed() -> None:
    md = "\n\n\n正文一段以句号结尾。\n"
    ir = _ir(md)
    patch = propose_blank_lines(ir).patches[0]
    from dataclasses import replace as _replace

    tampered = _replace(ir, meta=_replace(ir.meta, prefix="\n\n"))
    result = apply_patches(tampered, [patch], started_at=0, finished_at=0)
    assert result.patches[0].status == "failed"
    assert result.patches[0].skip_reason == "before_mismatch"
    assert result.md_after == render_ir(tampered)


def test_prefix_revert_refuses_after_a_manual_edit() -> None:
    md = "\n\n\n正文一段以句号结尾。\n"
    ir = _ir(md)
    result = apply_patches(ir, propose_blank_lines(ir).patches,
                           started_at=0, finished_at=0)
    from dataclasses import replace as _replace

    tampered = _replace(result.ir, meta=_replace(result.ir.meta, prefix="\n\n\n\n\n"))
    undone = revert_patches(tampered, result.report, finished_at=0)
    assert undone.patches[0].status == "failed"
    assert undone.patches[0].skip_reason == "after_mismatch"
    assert undone.ir.meta.prefix == "\n\n\n\n\n"        # 一个字节都没动


def test_blanks_only_document_collapses_to_empty() -> None:
    """整篇只有空行:没有块可改,只有文首空白可丢 —— 结果与 legacy 同为 ""。"""
    md = "\n\n\n\n"
    assert _ir(md).editable_blocks == ()
    proposal = propose_blank_lines(_ir(md))
    assert [p.operation for p in proposal.patches] == ["set_prefix"]
    out, _ = _run(md)
    assert out == ""
    assert _legacy_join(md) == ""


def test_trailing_gap_at_end_of_document_is_compressed() -> None:
    md = "正文段落以句号结尾。\n\n\n\n"
    out = _assert_matches_legacy(md)
    assert out == "正文段落以句号结尾。\n\n"


def test_patch_ids_are_deterministic() -> None:
    first = propose_blank_lines(_ir())
    second = propose_blank_lines(_ir())
    assert [p.patch_id for p in first.patches] == [p.patch_id for p in second.patches]


def test_unknown_rule_id_is_refused_by_the_driver() -> None:
    with pytest.raises(ValueError):
        propose_rules(_ir(), ("not_a_rule",))


# ---------------------------------------------------------------- 已知差异:注释内部

def test_multiline_comment_keeps_its_own_blank_lines() -> None:
    """多行 HTML 注释的内部空行:legacy 会压,IR 保留 —— 有意保留的差异。

    理由:注释是**原子块**,块 ``text`` 一律不动。这条约束同时保护着代码围栏、
    表格、脚注与公式(它们内部换行是内容)。注释内容不参与渲染,压不压都不改变
    产物语义,所以这里宁可**少改**一个字节,也不给块内改写开一个口子。
    不静默:能压的时候 ``notes`` 会写明"legacy 会压、IR 保留"。
    """
    md = "正文一段以句号结尾。\n\n<!-- 注:\n\n\n\n这一段在注释里 -->\n\n正文二段。\n"
    out, _reports = _run(md)
    assert "\n\n\n\n这一段在注释里" in out              # IR 保留原字节
    assert "\n\n这一段在注释里" in _legacy(md)          # legacy 压成 2
    assert out != _legacy(md)
    notes = propose_blank_lines(_ir(md)).notes
    assert any("注释块" in note for note in notes)


def test_single_line_comment_is_untouched_by_both() -> None:
    md = "正文一段以句号结尾。\n\n<!-- 单行注释 -->\n\n正文二段。\n"
    out, _ = _run(md)
    assert out == md
