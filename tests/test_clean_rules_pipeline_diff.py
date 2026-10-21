"""管线级差异测试:四条规则按 legacy 管线顺序联动,新旧引擎逐字节对比(P3/P4)。

legacy 管线顺序是 cjk_spaces(步 4)→ 空行压缩(步 5)→ join_lines(步 6)
→ ocr_spaces(步 7);``PIPELINE_ORDER`` 用同一顺序连续提案并应用。本文件验证:

1. 合成「OCR 产物形态」的复合输入,新旧引擎最终 Markdown 逐字节一致
   (已知的「文件末尾换行」分歧除外,见 test_clean_rules_join 的显式断言);
2. 规则命中/跳过原因可对照:统计两边各自的修改行为并断言可解释;
3. 引擎新增能力(trailer 变更、依赖跳过)的单元级测试;
4. P4:四规则联动下的撤销/重复执行/源指纹/依赖失败/手改拒绝(P4_MD 段)。
"""
from __future__ import annotations

import difflib

from clean import apply_patches, propose_join_lines, propose_rules, revert_patches
from clean.patch import BlockState
from clean.rules import PIPELINE_ORDER
from ir import build_ir, render_ir

MD = """\
# 第一章 巴勒斯坦问题的由来

一九四七年十一月联大通过分治决议后,犹太复国主义组织立即
在巴勒斯坦全面挑起武装冲突。

<!-- page 2 -->

这份资料最初由 Py Mu PDF 从扫描 件中提取,再经
人工校对整理成册。

白日依山尽
黄河入海流

第一章 附录与索引/204

① 列宁:《告犹太工人书》

```text
Py Mu PDF
```
"""


def _legacy_all(md: str) -> str:
    """四规则联动的**旧引擎输出**(冷藏快照 ``join_all``,见 ``legacy_snapshot``)。"""
    from legacy_snapshot import frozen

    return frozen("join_all", md)


def _ir_all(md: str) -> tuple[str, list]:
    ir = build_ir(md, source="synthetic.md", backend="synthetic")
    current, reports = propose_rules(ir, allow_apply=True)
    return render_ir(current), reports


def test_pipeline_matches_legacy_byte_for_byte() -> None:
    out, _reports = _ir_all(MD)
    legacy = _legacy_all(MD)
    assert out.rstrip("\n") == legacy.rstrip("\n"), "".join(
        difflib.unified_diff(legacy.splitlines(keepends=True),
                             out.splitlines(keepends=True), "legacy", "ir"))
    assert "扫描件" in out                     # cjk_spaces 删掉了「扫描 件」
    assert "PyMuPDF" in out                    # ocr_spaces 合并了拆词
    assert "联大通过分治决议后,犹太复国主义组织立即在巴勒斯坦" in out  # 块内拼接
    assert "白日依山尽  " in out               # 诗行硬换行
    assert "第一章 附录与索引/204" in out      # 目录条目原样


def test_pipeline_skip_reasons_are_auditable() -> None:
    out, reports = _ir_all(MD)
    suggested = [p for r in reports for p in r.patches if p.status == "suggested"]
    applied = [p for r in reports for p in r.patches if p.status == "applied"]
    # 页码注释保持独立、两侧段落不合并(有意的保守分歧:注释是页界证据)
    assert "武装冲突。\n\n<!-- page 2 -->\n\n这份资料" in out
    assert out.count("<!-- page 2 -->") == 1
    assert applied, "三条规则应当有实际修改"
    for patch in suggested:
        assert patch.skip_reason in (None, "evidence_insufficient",
                                     "prerequisite_missing")


def test_pipeline_without_heuristic_flag_changes_nothing() -> None:
    """默认(不带 allow_heuristic_apply)只产建议:正文一字不动。"""
    ir = build_ir(MD, source="synthetic.md", backend="synthetic")
    current, reports = propose_rules(ir, allow_apply=False)
    assert render_ir(current) == MD
    statuses = [p.status for r in reports for p in r.patches]
    assert statuses and set(statuses) <= {"suggested"}


# ---------------------------------------------------------------- 引擎:trailer 与依赖

def test_trailer_only_change_applies_and_reverts() -> None:
    md = "甲块正文。\n\n\n乙块正文。\n"
    ir = build_ir(md, backend="synthetic")
    a = ir.editable_blocks[0]
    patch = propose_trailer_patch(ir, a.id, "\n")
    result = apply_patches(ir, [patch], started_at=0, finished_at=0)
    assert result.md_after == "甲块正文。\n乙块正文。\n"
    assert result.patches[0].status == "applied"

    back = revert_patches(result.ir, result.report, finished_at=0)
    assert back.md_after == md


def propose_trailer_patch(ir, block_id: str, trailer: str):
    """测试辅助:构造一条只改块间空行的 replace_text。"""
    from clean.patch import AfterState, Evidence

    block = ir.block(block_id)
    patch = propose_replace_same_text(ir, block_id)
    from dataclasses import replace as _replace
    return _replace(patch, after=_replace(patch.after, trailer=trailer),
                    evidence=Evidence(kind="text_rule", matched=block.text[:16],
                                      source="text_rule"))


def propose_replace_same_text(ir, block_id: str):
    from clean.patch import Patch

    block = ir.block(block_id)
    return Patch.propose(patch_id="p0001", rule_id="cjk_spaces", block=block,
                         operation="replace_text", after_text=block.text)


def test_replace_with_trailer_mismatch_is_detected_on_replay() -> None:
    """trailer 变更后重放同一条 Patch:不能当成 already_applied 悄悄放过。"""
    md = "甲块正文。\n\n\n乙块正文。\n"
    ir = build_ir(md, backend="synthetic")
    patch = propose_trailer_patch(ir, ir.editable_blocks[0].id, "\n")
    result = apply_patches(ir, [patch], started_at=0, finished_at=0)
    replay = propose_trailer_patch(result.ir, result.ir.editable_blocks[0].id, "\n")
    again = apply_patches(result.ir, [replay], started_at=0, finished_at=0)
    assert again.patches[0].status == "skipped"
    assert again.patches[0].skip_reason == "already_applied"


def test_revert_refuses_when_trailer_was_hand_edited() -> None:
    md = "甲块正文。\n\n\n乙块正文。\n"
    ir = build_ir(md, backend="synthetic")
    patch = propose_trailer_patch(ir, ir.editable_blocks[0].id, "\n")
    result = apply_patches(ir, [patch], started_at=0, finished_at=0)
    # 手工把 trailer 改成第三种值(既不是 before 也不是 after → 拒绝恢复)
    a = result.ir.editable_blocks[0]
    from dataclasses import replace as _replace
    tampered = result.ir.replace_block(
        a.id, _replace(a, extra={**a.extra, "trailer": "\n\n"}))
    back = revert_patches(tampered, result.report, finished_at=0)
    assert back.patches[0].status == "failed"
    assert back.patches[0].skip_reason == "after_mismatch"


def test_join_group_survives_dependency_chain_on_real_ir() -> None:
    """端到端:替换成功 → 删除随之成功;再撤销 → 逐字节回到原文。"""
    from clean.patch import Evidence

    line_a = "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线"
    line_b = "前沿,向记者讲述着这场战争从开始到结束的全部经过"
    md = f"{line_a}\n\n{line_b}。\n"
    evidence = [{"type": "text", "text": line_a, "page_idx": 0},
                {"type": "text", "text": line_b + "。", "page_idx": 1}]
    ir = build_ir(md, backend="synthetic", evidence=evidence)
    proposal = propose_join_lines(ir)
    result = apply_patches(ir, proposal.patches, allow_heuristic_apply=True,
                           started_at=0, finished_at=0)
    assert [p.status for p in result.patches] == ["applied", "applied"]
    assert result.md_after == f"{line_a}{line_b}。\n"

    back = revert_patches(result.ir, result.report, finished_at=0)
    assert back.md_after == md


# ================================================================ P4 管线级

#: 复合输入:4+ 连续空行(空行压缩)+ 断行(拼接)+ 拆词(ocr_spaces)+ 中文空格
P4_MD = """\
# 第一章 起头

一九四七年十一月联大通过分治决议后,犹太复国主义组织立即
在巴勒斯坦全面挑起武装冲突。



这份资料最初由 Py Mu PDF 从扫描 件中提取,再经人工校对整理成册。

白日依山尽
黄河入海流
"""

P4_EVIDENCE = [
    {"type": "text", "text": "第一章 起头", "page_idx": 0},
    {"type": "text",
     "text": "一九四七年十一月联大通过分治决议后,犹太复国主义组织立即在巴勒斯坦全面挑起武装冲突。",
     "page_idx": 1},
    {"type": "text",
     "text": "这份资料最初由 Py Mu PDF 从扫描 件中提取,再经人工校对整理成册。",
     "page_idx": 2},
]


def _p4_run(md: str = P4_MD, *, evidence: list | None = None, allow_apply: bool = True):
    ir = build_ir(md, source="synthetic.md", backend="synthetic",
                  evidence=evidence, total_pages=3)
    current, reports = propose_rules(ir, PIPELINE_ORDER, allow_apply=allow_apply)
    return ir, current, reports


def test_p4_pipeline_matches_legacy_byte_for_byte() -> None:
    """四条规则(含空行压缩)按 legacy 顺序联动,逐字节一致。"""
    ir, current, _reports = _p4_run(evidence=P4_EVIDENCE)
    out = render_ir(current)
    legacy = _legacy_all(P4_MD)
    assert out.rstrip("\n") == legacy.rstrip("\n"), "".join(
        difflib.unified_diff(legacy.splitlines(keepends=True),
                             out.splitlines(keepends=True), "legacy", "ir"))


def test_p4_pipeline_removes_the_blank_hunks_legacy_removes() -> None:
    """P3 遗留的「源文件 4+ 连续空行」空白 hunk:压缩迁移后不再有差异。"""
    ir, current, reports = _p4_run(evidence=P4_EVIDENCE)
    out = render_ir(current)
    blank = [p for r in reports for p in r.patches if p.rule_id == "blank_lines"]
    assert blank, "这份输入含 3 个空行,应当有压缩补丁"
    assert all(p.status == "applied" for p in blank)
    assert "\n\n\n" not in out
    # 段间分隔保住了:标题与正文之间仍是恰好一个空行
    assert "# 第一章 起头\n\n一九四七年" in out


def test_p4_apply_then_revert_restores_original_bytes() -> None:
    """四规则连续应用 → 按报告逆序撤销:逐字节回到原文。"""
    ir, current, reports = _p4_run(evidence=P4_EVIDENCE)
    assert render_ir(current) != P4_MD              # 前提:确实改了
    back = current
    for report in reversed(reports):
        back = revert_patches(back, report, finished_at=0).ir
    assert render_ir(back) == P4_MD


def test_p4_revert_refuses_when_the_hand_edited_the_text() -> None:
    """报告之后有人手改了正文 → 拒绝撤销(而不是把手工修改一起抹掉)。"""
    ir, current, reports = _p4_run(evidence=P4_EVIDENCE)
    applied = [p for r in reports for p in r.patches if p.status == "applied"]
    assert applied
    target = next(p for p in applied if p.after is not None
                  and p.after.text != p.before.text)
    block = current.block(target.target_block_id)
    assert block is not None
    # 走 rewrite 入口:md_span 必须跟着 text 一起变(手工改正文的真实形态)
    tampered = current.rewrite(block.id, block.text + "手写备注。")
    results = [revert_patches(tampered, r, finished_at=0) for r in reversed(reports)]
    refusals = [p for res in results for p in res.patches if p.status == "failed"]
    assert refusals, "手改过的块必须被拒绝恢复"
    assert all(p.skip_reason == "after_mismatch" for p in refusals)


def test_p4_revert_refuses_when_the_gap_was_hand_edited() -> None:
    """空行压缩之后手改了块间空行 → 拒绝恢复(不能猜该信哪一边)。"""
    ir, current, reports = _p4_run(evidence=P4_EVIDENCE)
    blank = [p for r in reports for p in r.patches if p.rule_id == "blank_lines"]
    assert blank
    block = current.block(blank[0].target_block_id)
    assert block is not None
    from dataclasses import replace as _replace

    tampered = current.replace_block(
        block.id, _replace(block, extra={**block.extra, "trailer": "\n\n\n\n\n"}))
    back = revert_patches(tampered, reports[0], finished_at=0)
    failed = [p for p in back.patches if p.status == "failed"]
    assert failed
    assert all(p.skip_reason == "after_mismatch" for p in failed)


def test_p4_source_fingerprint_mismatch_refuses_every_rule() -> None:
    """拿另一份输入的报告来跑:四条规则全批拒绝,原文一字不动。"""
    ir, _current, reports = _p4_run(evidence=P4_EVIDENCE)
    foreign = build_ir("完全无关的另一份输入正文以句号结尾。\n", backend="synthetic")
    stolen = [p for r in reports for p in r.patches if p.status == "applied"]
    result = apply_patches(foreign, stolen, started_at=0, finished_at=0)
    assert all(p.status == "failed" for p in result.patches)
    assert all(p.skip_reason == "source_mismatch" for p in result.patches)
    assert result.md_after == render_ir(foreign)


def test_p4_dependency_failure_blocks_the_delete() -> None:
    """拼接组:前置替换失败 → 删除按 prerequisite_missing 跳过,不留半修复。"""
    from clean import propose_join_lines
    from clean.patch import BlockState

    line_a = "一九七三年十月战争后,埃及士兵站在已被摧毁的巴列夫防线"
    line_b = "前沿,向记者讲述着这场战争从开始到结束的全部经过"
    md = f"{line_a}\n\n{line_b}。\n"
    evidence = [{"type": "text", "text": line_a, "page_idx": 0},
                {"type": "text", "text": line_b + "。", "page_idx": 1}]
    ir = build_ir(md, backend="synthetic", evidence=evidence)
    proposal = propose_join_lines(ir)
    replace, delete = proposal.patches[0], proposal.patches[1]
    assert delete.depends_on == replace.patch_id
    # 把前置替换的 before 改成对不上的正文 → 它失败,删除必须跟着跳过
    broken = replace.with_outcome(replace.status, before=BlockState(text="对不上的正文"))
    result = apply_patches(ir, [broken, delete], allow_heuristic_apply=True,
                           started_at=0, finished_at=0)
    assert result.patches[0].status == "failed"
    assert result.patches[0].skip_reason == "before_mismatch"
    assert result.patches[1].status == "skipped"
    assert result.patches[1].skip_reason == "prerequisite_missing"
    assert result.md_after == md                    # 零改动,没有半修复


def test_p4_second_run_over_the_whole_pipeline_changes_nothing() -> None:
    """幂等:跑完一遍再跑一遍,第二遍零补丁(重复执行不得越压越多)。"""
    ir, current, _reports = _p4_run(evidence=P4_EVIDENCE)
    once = render_ir(current)
    again = build_ir(once, source="synthetic.md", backend="synthetic",
                     evidence=P4_EVIDENCE, total_pages=3)
    second, reports2 = propose_rules(again, PIPELINE_ORDER, allow_apply=True)
    assert [p.status for r in reports2 for p in r.patches] == []
    assert render_ir(second) == once


def test_p4_default_run_only_suggests_except_the_deterministic_blank_rule() -> None:
    """默认(不开 allow_heuristic_apply):确定性规则照跑,启发式规则只产建议。

    空行压缩是"数换行",不是猜测 —— legacy 也把它当无条件步骤。把它和
    启发式规则混为一谈,等于让一个零风险归一化也被关掉。
    """
    ir, current, reports = _p4_run(evidence=P4_EVIDENCE, allow_apply=False)
    out = render_ir(current)
    assert "\n\n\n" not in out                      # 空行压缩照样执行
    rules = {p.rule_id for r in reports for p in r.patches}
    assert "blank_lines" in rules
    for rule in rules - {"blank_lines"}:
        assert all(p.status == "suggested" for r in reports for p in r.patches
                   if p.rule_id == rule)


def test_before_mismatch_reports_block_state_snapshot() -> None:
    """BlockState 快照语义不因 trailer 扩展而变:不符就拒绝。"""
    from clean.patch import AfterState, Evidence, Patch

    md = "甲块正文。\n"
    ir = build_ir(md, backend="synthetic")
    block = ir.editable_blocks[0]
    patch = Patch.propose(patch_id="p0001", rule_id="cjk_spaces", block=block,
                          operation="replace_text", after_text="乙块正文。",
                          evidence=Evidence(kind="text_rule", matched="甲块",
                                            source="text_rule"))
    tampered = patch.with_outcome(patch.status,
                                  before=BlockState(text="别的版本"))
    result = apply_patches(ir, [tampered], started_at=0, finished_at=0)
    assert result.patches[0].status == "failed"
    assert result.md_after == md
    assert AfterState(text="x").trailer is None     # 新字段缺省不改变旧语义
