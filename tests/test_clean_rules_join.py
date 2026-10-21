"""join_lines(+ 诗行硬换行)迁移到 Patch 体系后的行为测试(P3)。

覆盖面(只测该规则实际适用的边界):
  - 正常修复:块内断行拼接、跨空行拼接(与 legacy 逐字节一致);
  - 证据不足:被吸收块没有结构证据 → 只产建议,正文一字不动;
  - 依赖链:前置替换失败 → 删除按 ``prerequisite_missing`` 跳过,不留半修复;
  - 重复执行:第二次运行零提案(幂等);
  - 跨页注释:有证据层时照样拼接,注释保持独立、原位(P6 收紧后,缺证据的
    跨块改动只留建议;见下面两条显式断言);
  - 诗行/目录条目/版式字段行:不拼接 + 硬换行;
  - 脚注/公式/代码块:整体不触碰;
  - 撤销:apply → revert 逐字节还原(含被吸收块与块间空行)。

夹具全部合成,不含真实书籍正文。

为什么这些用例还要和 legacy 逐字节比对:这批行为是在 P1–P5 用真实书反复校准出来的,
把预期写成「冷藏的旧引擎输出快照」(``tests/fixtures/legacy_snapshot.json``)可以让
"新引擎和旧行为一致"这件事继续被机器盯着 —— 旧清理器已在 P6 删除,留下的只有它
被验证过的输出,不再有两套实现。
"""
from __future__ import annotations

import difflib

import pytest

from clean import apply_patches, propose_join_lines, revert_patches
from ir import build_ir, render_ir

# 断行句:两行都 ≥20 字(排满一行)、前一行无句末标点 → legacy 判拼接
LINE_A = "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线"
LINE_B = "前沿,向记者讲述着这场战争从开始到结束的全部经过"

MD = f"""\
# 第一章 起头

{LINE_A}
{LINE_B}

这是第二段被空行隔开的断句前半部分内容足够长无标点

后半句紧跟着出现在下一个块里。
"""


def _ir(md: str = MD, *, evidence: list | None = None, **kwargs):
    return build_ir(md, source="synthetic.md", backend="synthetic",
                    evidence=evidence, **kwargs)


def _run(md: str = MD, *, evidence: list | None = None):
    """提案 + 强制应用(人工确认后的重跑语义),返回 (渲染结果, 报告列表)。"""
    ir = _ir(md, evidence=evidence)
    proposal = propose_join_lines(ir)
    if not proposal.patches:
        return md, []
    result = apply_patches(ir, proposal.patches, allow_heuristic_apply=True,
                           started_at=0, finished_at=0)
    return render_ir(result.ir), [result.report]


def _legacy(md: str) -> str:
    """同一输入只开 join_lines 的**旧引擎输出**(冷藏快照 ``join_only``)。

    旧清理器已在 P6 删除;这里读的是它删除前录下来的输出(见
    ``tests/legacy_snapshot.py``),用途是继续盯着「新引擎与校准确的旧行为一致」。
    """
    from legacy_snapshot import frozen

    return frozen("join_only", md)


def _assert_matches_legacy(md: str, *, evidence: list | None = None) -> str:
    """新旧行为逐字节一致(已知分歧:文件末尾换行 legacy 会丢,IR 保留)。"""
    out, _reports = _run(md, evidence=evidence)
    assert out.rstrip("\n") == _legacy(md).rstrip("\n"), "".join(
        difflib.unified_diff(_legacy(md).splitlines(keepends=True),
                             out.splitlines(keepends=True), "legacy", "ir"))
    return out


# ---------------------------------------------------------------- 正常修复

#: 证据层夹具:断行两半在 content_list 里本就是两条结构块,各自带 bbox/页号
EVIDENCE_BOTH = [
    {"type": "text", "text": LINE_A, "page_idx": 0, "bbox": [0, 0, 100, 20]},
    {"type": "text", "text": LINE_B + "。", "page_idx": 1, "bbox": [0, 20, 100, 40]},
]


def test_within_block_broken_lines_are_joined() -> None:
    out, reports = _run()
    assert f"{LINE_A}{LINE_B}" in out
    # 第二个断句没有任何证据 → 两处被吸收块都只产建议,不动正文
    statuses = [p.status for p in reports[0].patches]
    assert statuses == ["applied", "suggested", "suggested"]


def test_cross_blank_join_merges_blocks_and_deletes_the_absorbed_one() -> None:
    md = (f"{LINE_A}\n\n{LINE_B}。\n")
    out, reports = _run(md, evidence=EVIDENCE_BOTH)
    assert out == f"{LINE_A}{LINE_B}。\n"
    kinds = [(p.operation, p.status) for p in reports[0].patches]
    assert ("replace_text", "applied") in kinds
    assert ("delete_block", "applied") in kinds
    # delete 带显式依赖,依赖的是同一次运行里的 replace
    delete = next(p for p in reports[0].patches if p.operation == "delete_block")
    replace = next(p for p in reports[0].patches if p.operation == "replace_text")
    assert delete.depends_on == replace.patch_id


def test_absorber_trailer_takes_over_the_absorbed_block_trailer() -> None:
    """拼接吃掉的是 A、B 之间的空行;B 之后的空行要跟到 A 身上。"""
    md = f"{LINE_A}\n\n{LINE_B}。\n\n下一段以句号结尾。\n"
    evidence = [
        {"type": "text", "text": LINE_A, "page_idx": 0},
        {"type": "text", "text": LINE_B + "。", "page_idx": 1},
    ]
    out, _reports = _run(md, evidence=evidence)
    assert out == f"{LINE_A}{LINE_B}。\n\n下一段以句号结尾。\n"


def test_full_join_pipeline_matches_legacy_byte_for_byte() -> None:
    """复合夹具:块内 + 跨空行 + 诗行 + 目录 + 版式行 + 代码 + 脚注 + 公式。"""
    md = (
        f"# 第一章 起头\n"
        f"\n"
        f"{LINE_A}\n"
        f"{LINE_B}\n"
        f"\n"
        f"白日依山尽\n"
        f"黄河入海流\n"
        f"\n"
        f"第一章 失踪与疯癫/35\n"
        f"\n"
        f"人民出版社出版 新華書店发行\n"
        f"\n"
        f"[^1]: 列宁:《告犹太工人书》,人民出版社一九五九年版。\n"
        f"\n"
        f"$$\n"
        f"E = mc^2\n"
        f"$$\n"
        f"\n"
        f"```python\n"
        f"x = 1\n"
        f"```\n"
        f"\n"
        f"正文最后一段,以句号收尾保证不再触发任何规则。\n"
    )
    out = _assert_matches_legacy(md)
    assert f"{LINE_A}{LINE_B}" in out
    assert "白日依山尽  \n" in out          # 诗行硬换行
    assert "第一章 失踪与疯癫/35" in out     # 目录条目原样
    assert "人民出版社出版 新華書店发行" in out  # 版式行原样
    assert "[^1]: 列宁" in out               # 脚注原样
    assert "$$\nE = mc^2\n$$" in out         # 公式原样
    assert "```python\nx = 1\n```" in out    # 代码原样


def test_multilingual_join_inserts_a_space_like_legacy() -> None:
    md = "The quick brown fox jumps over the lazy dog near\nthe river bank every morning.\n"
    out = _assert_matches_legacy(md)
    assert "near the river" in out


# ---------------------------------------------------------------- 证据与依赖

def test_cross_join_without_evidence_is_only_a_suggestion() -> None:
    """被吸收块没有结构证据 → 只记建议,正文一字不动(防止「并了文没删块」)。"""
    ir = _ir(f"{LINE_A}\n\n{LINE_B}。\n")
    proposal = propose_join_lines(ir)
    result = apply_patches(ir, proposal.patches, started_at=0, finished_at=0)
    assert result.md_after == f"{LINE_A}\n\n{LINE_B}。\n"
    delete = next(p for p in result.patches if p.operation == "delete_block")
    assert delete.status == "suggested"
    assert delete.skip_reason == "evidence_insufficient"


def test_failed_replace_blocks_the_delete_via_dependency() -> None:
    """前置替换失败(before 不符)→ 删除按 prerequisite_missing 跳过,零改动。"""
    md = f"{LINE_A}\n\n{LINE_B}。\n"
    ir = _ir(md, evidence=EVIDENCE_BOTH)
    proposal = propose_join_lines(ir)
    # 篡改 replace 的 before,让它在应用期失败
    tampered = [p for p in proposal.patches if p.operation == "replace_text"]
    from clean.patch import BlockState
    tampered = [p.with_outcome(p.status, before=BlockState(text="不是当前内容"))
                for p in tampered]
    others = [p for p in proposal.patches if p.operation != "replace_text"]
    result = apply_patches(ir, tampered + others, started_at=0, finished_at=0)

    assert result.md_after == md                  # 一个字符都没动
    assert all(p.status == "failed" for p in result.patches
               if p.operation == "replace_text")
    delete = next(p for p in result.patches if p.operation == "delete_block")
    assert delete.status == "skipped"
    assert delete.skip_reason == "prerequisite_missing"


def test_replaying_the_whole_proposal_is_idempotent() -> None:
    evidence = EVIDENCE_BOTH
    once, _ = _run(MD, evidence=evidence)
    twice, reports = _run(once, evidence=evidence)
    assert twice == once
    assert all(not r.patches or
               all(p.status in ("suggested", "skipped") for p in r.patches)
               for r in reports)


def test_apply_then_revert_restores_original_bytes() -> None:
    md = f"# 章\n\n{LINE_A}\n\n{LINE_B}。\n"
    ir = _ir(md, evidence=EVIDENCE_BOTH)
    proposal = propose_join_lines(ir)
    result = apply_patches(ir, proposal.patches, allow_heuristic_apply=True,
                           started_at=0, finished_at=0)
    assert result.md_after != md
    back = revert_patches(result.ir, result.report, finished_at=0)
    assert back.md_after == md
    assert render_ir(back.ir) == md


# ---------------------------------------------------------------- 已知分歧(显式断言,不静默)

def test_join_across_page_marker_merges_and_marker_stays_in_place() -> None:
    """跨页码注释的拼接:正文合并了,注释**还是独立的块**(位置和文字都不变)。

    注释只是渲染顺序落在合并后的段落之后 —— 它是页界证据,不是内容,所以:

      - 合并后的段落留在注释**之前**(它本来从上一页就开始了);旧引擎把它搬到注释
        之后,产物因此谎报"这段从第 21 页开始";
      - 注释一条不少、一个字符没改,位置也不随合并漂移(``page_markers`` golden
        样本把这条钉住了)。
    """
    md = (f"{LINE_A}\n<!-- page 2 -->\n{LINE_B}。\n")
    evidence = [{"type": "text", "text": LINE_A, "page_idx": 0},
                {"type": "text", "text": LINE_B + "。", "page_idx": 1}]
    out, _reports = _run(md, evidence=evidence)
    assert f"{LINE_A}{LINE_B}。" in out          # 两个半句合成一段
    assert out.count("<!-- page 2 -->") == 1     # 注释还是那一条
    assert out.endswith("。\n<!-- page 2 -->\n")


def test_join_across_page_marker_without_evidence_changes_nothing() -> None:
    """同一份输入没有证据层:跨块删除不立得住,只留建议。

    这是 P6 的闸门:跨块改动必须有结构证据;没有证据就"先看不动",而不是降低门槛。
    """
    md = (f"{LINE_A}\n<!-- page 2 -->\n{LINE_B}。\n")
    out, reports = _run(md, evidence=None)
    assert out == md
    suggested = [p for r in reports for p in r.patches if p.status == "suggested"]
    assert suggested and all(p.skip_reason == "evidence_insufficient"
                             for p in suggested)


def test_ir_preserves_the_trailing_newline_legacy_drops() -> None:
    """legacy 拼接会把文件末尾换行丢掉;IR 版保留(IR 的行为是正确的)。"""
    md = f"{LINE_A}\n{LINE_B}。\n"
    out, _reports = _run(md, evidence=[{"type": "text", "text": LINE_A}])
    assert out.endswith("。\n")
    assert not _legacy(md).endswith("\n")


# ---------------------------------------------------------------- 诗行与结构行

def test_verse_lines_get_hard_breaks_and_are_never_joined() -> None:
    md = "白日依山尽\n黄河入海流\n"
    out = _assert_matches_legacy(md)
    assert out == "白日依山尽  \n黄河入海流\n"


def test_verse_run_across_a_page_marker_still_gets_breaks() -> None:
    """mark 对诗行连续性透明(legacy 同款),但 mark 行本身不加换行。"""
    md = "白日依山尽\n<!-- page 2 -->\n黄河入海流\n"
    out = _assert_matches_legacy(md)
    assert out == "白日依山尽  \n<!-- page 2 -->\n黄河入海流\n"


def test_single_short_line_between_blanks_is_left_alone() -> None:
    md = "独立的一小行\n"
    assert _assert_matches_legacy(md) == md


def test_catalog_and_note_lines_are_never_joined() -> None:
    md = ("第一章 失踪与疯癫/35\n"
          "第二章 颠倒的世界/52\n")
    out = _assert_matches_legacy(md)
    assert "第一章 失踪与疯癫/35" in out and "第二章" in out
    assert "/35第二章" not in out


def test_layout_field_line_is_not_joined() -> None:
    md = ("人民出版社出版\n新華書店发行\n")
    out = _assert_matches_legacy(md)
    assert "人民出版社出版" in out and "新華書店发行" in out


def test_short_predecessor_does_not_join() -> None:
    """前一行不足 20 字(没排满)就不拼 —— 上一行是标题/说明时防误伤。"""
    md = "中东僵局\n"
    assert _assert_matches_legacy(md) == md


# ---------------------------------------------------------------- 结构保护

def test_code_table_math_footnote_blocks_are_never_touched() -> None:
    md = ("```text\nline one\nline two\n```\n"
          "\n"
          "| 甲 | 乙 |\n"
          "| -- | -- |\n"
          "\n"
          "$$\ny = x + 1\n$$\n"
          "\n"
          "[^2]: 注:内容来自同一本书的附录。\n")
    out = _assert_matches_legacy(md)
    assert "```text\nline one\nline two\n```" in out
    assert "| 甲 | 乙 |" in out
    assert "$$\ny = x + 1\n$$" in out
    assert "[^2]: 注:内容来自同一本书的附录。" in out


def test_line_starting_with_inline_code_joins_like_legacy() -> None:
    """行内代码占位符不带 ``<`` 包装(不是块结构),行首行内代码 legacy 会拼。"""
    md = (f"{LINE_A}\n"
          f"`git 命令` 后面的内容足够长而且没有句末标点\n")
    out = _assert_matches_legacy(md)
    assert f"{LINE_A}`git 命令`" in out


# ---------------------------------------------------------------- 提案可审计

def test_proposal_notes_report_what_happened() -> None:
    evidence = [{"type": "text", "text": LINE_A, "page_idx": 0}]
    ir = _ir(MD, evidence=evidence)
    proposal = propose_join_lines(ir)
    assert any("跨块拼接" in n for n in proposal.notes)
    assert proposal.rule_id == "join_lines"


def test_proposal_on_clean_text_is_empty() -> None:
    md = "第一段以句号结尾。\n\n第二段也以句号结尾。\n"
    ir = _ir(md)
    proposal = propose_join_lines(ir)
    assert proposal.patches == ()
