"""Patch 应用与撤销(阶段 B §E.7 的双校验规则)。

这一组测试全部围绕「报告还算不算数」:

  - 正常应用:改对了,``after.sha256`` 校验通过;
  - ``before`` 不符 / 目标块不存在 / 输入指纹不符:**一个字符都不动**;
  - 重复应用:第二次必须认出来,不能把同一处改两遍;
  - 撤销:必须逐字节回到原文(含块间空行、层级);
  - 撤销前 ``after`` 不符(报告之后有人手改过):拒绝,不抹掉手工修改;
  - 同块多条 Patch 必须按执行顺序链式校验;
  - 低置信度/证据不足:只记建议,不动正文。

夹具是合成 Markdown,不含任何真实书籍正文。
"""
from __future__ import annotations

import pytest

from clean import (Patch, PatchError, SourceFingerprint, apply_patches,
                   default_options, read_report, render_unified_diff,
                   revert_patches, write_report)
from clean.patch import AfterState, BlockState, Evidence
from ir import build_ir, render_ir, text_sha256

MD = """\
# 第一章 起头

正文第一段,句尾没有标点。

17

第二段正文,句尾没有标点

他接在这一行的后面

185
"""

#: 证据块样本:与 MD 首段同文,用来验证"证据块不可改写"
EVIDENCE = [
    {"type": "text", "text": "正文第一段,句尾没有标点。", "page_idx": 0,
     "bbox": [0, 0, 100, 20]},
]


def _ir(md: str = MD, **kwargs):
    return build_ir(md, source="synthetic.md", backend="mineru",
                    total_pages=3, **kwargs)


def _block(ir, needle: str):
    return next(b for b in ir.editable_blocks if needle in b.text)


def _run(ir, patches, **kwargs):
    kwargs.setdefault("started_at", 0)
    kwargs.setdefault("finished_at", 0)
    return apply_patches(ir, patches, **kwargs)


def _delete_page_number(ir, patch_id: str = "p0001", **evidence) -> Patch:
    data = dict(kind="page_number", matched="17", source="evidence_block")
    data.update(evidence)
    return Patch.propose(patch_id=patch_id, rule_id="page_numbers",
                         block=_block(ir, "17"), operation="delete_block",
                         evidence=Evidence(**data))


def _replace_body(ir, patch_id: str = "p0002", text: str = "第二段正文，句尾没有标点",
                  **kwargs) -> Patch:
    return Patch.propose(patch_id=patch_id, rule_id="cjk_spaces",
                         block=_block(ir, "第二段"), operation="replace_text",
                         after_text=text,
                         evidence=Evidence(kind="text", matched="正文,", source="md_block"),
                         **kwargs)


# ---------------------------------------------------------------- 正常路径

def test_replace_text_applies_and_records_validation() -> None:
    ir = _ir()
    patch = _replace_body(ir)
    result = _run(ir, [patch])

    assert result.patches[0].status == "applied"
    assert result.patches[0].skip_reason is None
    assert result.patches[0].validation.before_ok
    assert result.patches[0].validation.after_ok
    assert "第二段正文，句尾没有标点" in result.md_after
    assert result.report.verification.roundtrip_ok
    # 报告自证输入:source_fingerprint 由引擎补上
    stamp = result.patches[0].source_fingerprint
    assert stamp is not None and stamp.book_md_sha256 == text_sha256(MD)


def test_delete_block_removes_line_and_its_blank_line() -> None:
    ir = _ir()
    result = _run(ir, [_delete_page_number(ir)])

    assert result.patches[0].status == "applied"
    assert "\n17\n" not in result.md_after
    assert result.md_after == """\
# 第一章 起头

正文第一段,句尾没有标点。

第二段正文,句尾没有标点

他接在这一行的后面

185
"""
    assert result.report.stats.blocks_after == result.report.stats.blocks_before - 1


def test_noop_records_the_rule_looked_and_did_nothing() -> None:
    ir = _ir()
    block = _block(ir, "正文第一段")
    patch = Patch.propose(patch_id="p0001", rule_id="images", block=block,
                          operation="noop",
                          evidence=Evidence(kind="text", matched="正文第一段",
                                            source="md_block"))
    result = _run(ir, [patch])
    assert result.patches[0].status == "applied"
    assert result.md_after == MD


# ---------------------------------------------------------------- 校验失败

def test_before_hash_mismatch_changes_nothing() -> None:
    ir = _ir()
    patch = _replace_body(ir)
    tampered = patch.with_outcome(
        patch.status, before=BlockState(text="这段不是当前块的内容"))
    result = _run(ir, [tampered])

    assert result.patches[0].status == "failed"
    assert result.patches[0].skip_reason == "before_mismatch"
    assert result.patches[0].validation.before_ok is False
    assert result.md_after == MD


def test_missing_target_block_is_failed_not_guessed() -> None:
    ir = _ir()
    patch = _delete_page_number(ir)
    moved = Patch.from_dict({**patch.to_dict(), "target_block_id": "book#m99"})
    result = _run(ir, [moved])

    assert result.patches[0].status == "failed"
    assert result.patches[0].skip_reason == "target_missing"
    assert result.md_after == MD


def test_replaying_a_whole_report_is_refused_by_the_source_fingerprint() -> None:
    ir = _ir()
    patches = [_delete_page_number(ir), _replace_body(ir)]
    first = _run(ir, patches)
    assert [p.status for p in first.patches] == ["applied", "applied"]

    # 报告改过文档 → 文档指纹变了 → 同一份报告不再属于当前输入
    second = _run(first.ir, first.report.patches)
    assert [p.status for p in second.patches] == ["failed", "failed"]
    assert {p.skip_reason for p in second.patches} == {"source_mismatch"}
    assert second.report.verification.source_ok is False
    assert second.md_after == first.md_after


def test_replaying_a_single_patch_that_is_already_in_effect_is_skipped() -> None:
    ir = _ir()
    first = _run(ir, [_replace_body(ir)])
    replay = Patch.from_dict({**first.patches[0].to_dict(), "status": "candidate",
                              "source_fingerprint": None})
    second = _run(first.ir, [replay])

    assert second.patches[0].status == "skipped"
    assert second.patches[0].skip_reason == "already_applied"
    assert second.md_after == first.md_after


def test_deleting_an_already_deleted_block_is_skipped() -> None:
    ir = _ir()
    first = _run(ir, [_delete_page_number(ir)])
    replay = Patch.from_dict({**first.patches[0].to_dict(), "status": "candidate",
                              "source_fingerprint": None})
    second = _run(first.ir, [replay])

    assert second.patches[0].status == "skipped"
    assert second.patches[0].skip_reason == "already_applied"
    assert second.md_after == first.md_after


def test_source_fingerprint_mismatch_refuses_everything() -> None:
    ir = _ir()
    stale = SourceFingerprint(book_md_sha256="0" * 64, ir_schema=1,
                              backend="mineru", variant="original")
    patch = _replace_body(ir)
    patch = Patch.from_dict({**patch.to_dict(),
                             "source_fingerprint": stale.to_dict()})
    result = _run(ir, [patch])

    assert result.patches[0].status == "failed"
    assert result.patches[0].skip_reason == "source_mismatch"
    assert result.report.verification.source_ok is False
    assert result.md_after == MD


def test_readonly_evidence_block_is_rejected_and_counted() -> None:
    ir = _ir(evidence=EVIDENCE)
    evidence_block = ir.evidence_blocks[0]
    patch = Patch.propose(patch_id="p0007", rule_id="running_heads",
                          block=evidence_block, operation="delete_block",
                          evidence=Evidence(kind="text", matched=evidence_block.text,
                                            source="evidence_block"))
    result = _run(ir, [patch])

    assert result.patches[0].status == "rejected"
    assert result.patches[0].skip_reason == "readonly_block"
    assert result.report.verification.readonly_violations == 1
    assert result.md_after == MD


# ---------------------------------------------------------------- 低置信度

def test_heuristic_patch_is_only_a_suggestion() -> None:
    ir = _ir()
    patch = _replace_body(ir, confidence="heuristic", risk="medium")
    result = _run(ir, [patch])

    assert result.patches[0].status == "suggested"
    assert result.patches[0].skip_reason == "evidence_insufficient"
    assert result.md_after == MD


def test_delete_without_geometry_is_only_a_suggestion() -> None:
    ir = _ir()
    text_only = _delete_page_number(ir, source="text_rule")
    result = _run(ir, [text_only])

    assert result.patches[0].status == "suggested"
    assert result.patches[0].skip_reason == "evidence_insufficient"
    assert result.md_after == MD

    geometric = _delete_page_number(ir, source="text_rule",
                                    bbox=(60.0, 780.0, 200.0, 800.0),
                                    bbox_space="page_px")
    applied = _run(ir, [geometric])
    assert applied.patches[0].status == "applied"
    assert "\n17\n" not in applied.md_after


def test_heuristic_can_be_forced_only_explicitly() -> None:
    ir = _ir()
    patch = _replace_body(ir, confidence="heuristic")
    forced = _run(ir, [patch], allow_heuristic_apply=True)
    assert forced.patches[0].status == "applied"
    assert "第二段正文，句尾没有标点" in forced.md_after


# ---------------------------------------------------------------- 连续补丁

def test_sequential_patches_on_one_block_form_a_chain() -> None:
    ir = _ir()
    first = _replace_body(ir, patch_id="p0001", text="第二段正文(改一)")
    second = Patch.from_dict({
        **first.to_dict(),
        "patch_id": "p0002",
        "before": BlockState(text="第二段正文(改一)").to_dict(),
        "after": AfterState(text="第二段正文(改二)").to_dict(),
        "status": "candidate",
        "source_fingerprint": None,
    })
    result = _run(ir, [first, second])

    assert [p.status for p in result.patches] == ["applied", "applied"]
    assert _block(result.ir, "第二段").text == "第二段正文(改二)"
    # 链式:第二条的 before 必须等于第一条的 after
    assert result.patches[1].before.sha256 == result.patches[0].after.sha256


def test_broken_chain_is_refused() -> None:
    ir = _ir()
    first = _replace_body(ir, patch_id="p0001", text="第二段正文(改一)")
    second = Patch.from_dict({
        **first.to_dict(),
        "patch_id": "p0002",
        "before": BlockState(text="第二段正文(另一个版本)").to_dict(),
        "after": AfterState(text="第二段正文(改二)").to_dict(),
        "status": "candidate",
        "source_fingerprint": None,
    })
    result = _run(ir, [first, second])

    assert [p.status for p in result.patches] == ["applied", "failed"]
    assert result.patches[1].skip_reason == "before_mismatch"
    assert _block(result.ir, "第二段").text == "第二段正文(改一)"


def test_duplicate_patch_id_is_refused() -> None:
    ir = _ir()
    patch = _replace_body(ir)
    result = _run(ir, [patch, patch])
    assert [p.status for p in result.patches] == ["applied", "failed"]
    assert result.patches[1].skip_reason == "ambiguous_target"


# ---------------------------------------------------------------- 撤销

def test_revert_restores_original_bytes() -> None:
    ir = _ir()
    result = _run(ir, [_delete_page_number(ir), _replace_body(ir)])
    assert result.md_after != MD

    back = revert_patches(result.ir, result.report, finished_at=0)
    assert [p.status for p in back.patches] == ["applied", "applied"]
    assert back.md_after == MD
    assert render_ir(back.ir) == MD


def test_revert_refuses_after_the_text_was_edited_by_hand() -> None:
    ir = _ir()
    result = _run(ir, [_replace_body(ir)])
    hand_edited = result.ir.rewrite(_block(result.ir, "第二段").id,
                                    "有人手工改成了这一句")
    back = revert_patches(hand_edited, result.report, finished_at=0)

    assert back.patches[0].status == "failed"
    assert back.patches[0].skip_reason == "after_mismatch"
    assert "有人手工改成了这一句" in back.md_after       # 没被抹掉


def test_revert_is_idempotent() -> None:
    ir = _ir()
    result = _run(ir, [_delete_page_number(ir)])
    once = revert_patches(result.ir, result.report, finished_at=0)
    twice = revert_patches(once.ir, result.report, finished_at=0)

    assert twice.patches[0].status == "skipped"
    assert twice.patches[0].skip_reason == "already_reverted"
    assert twice.md_after == MD


def test_set_level_changes_only_the_level_and_reverts() -> None:
    ir = _ir()
    heading = _block(ir, "第一章")
    patch = Patch.propose(patch_id="p0003", rule_id="headings", block=heading,
                          operation="set_level", level=2,
                          evidence=Evidence(kind="heading", matched="第一章 起头",
                                            source="evidence_block", text_level=2))
    result = _run(ir, [patch])
    assert result.patches[0].status == "applied"
    assert result.md_after == MD                      # 正文没变,只改了层级
    assert _block(result.ir, "第一章").level == 2

    back = revert_patches(result.ir, result.report, finished_at=0)
    assert back.patches[0].status == "applied"
    assert _block(back.ir, "第一章").level == 1


def test_merge_blocks_is_refused_instead_of_leaving_an_unrevertable_state() -> None:
    ir = _ir()
    merged_text = _block(ir, "第二段").text + _block(ir, "他接在").text
    patch = Patch.propose(patch_id="p0004", rule_id="join_lines",
                          block=_block(ir, "第二段"), operation="merge_blocks",
                          after_text=merged_text,
                          evidence=Evidence(kind="text", matched="没有标点",
                                            source="geometry",
                                            bbox=(0, 0, 100, 20), bbox_space="page_px"))
    result = _run(ir, [patch])

    assert result.patches[0].status == "failed"
    assert result.patches[0].skip_reason == "operation_mismatch"
    assert "replace_text + delete_block" in result.patches[0].validation.notes[0]
    assert result.md_after == MD


# ---------------------------------------------------------------- 报告与展示层

def test_report_artifacts_roundtrip_on_disk(tmp_path) -> None:
    ir = _ir()
    result = _run(ir, [_delete_page_number(ir)])
    out = result.write(tmp_path)

    assert (out / "clean-report.json").is_file()
    assert (out / "clean.diff").is_file()
    assert read_report(out / "clean-report.json").to_json() == result.report.to_json()
    assert "-17" in (out / "clean.diff").read_text(encoding="utf-8")


def test_diff_is_display_only_and_never_used_for_revert(tmp_path) -> None:
    ir = _ir()
    result = _run(ir, [_delete_page_number(ir)])
    out = result.write(tmp_path)
    (out / "clean.diff").unlink()               # 展示层丢掉不影响撤销

    report = read_report(out / "clean-report.json")
    back = revert_patches(result.ir, report, finished_at=0)
    assert back.md_after == MD


def test_diff_is_empty_when_nothing_changed() -> None:
    assert render_unified_diff(MD, MD) == ""


def test_option_flags_are_recorded_not_invented() -> None:
    ir = _ir()
    options = default_options() | {"join_lines": False, "bold": True}
    result = _run(ir, [_delete_page_number(ir)], options=options,
                  rules_enabled=("page_numbers",),
                  rules_skipped=({"rule_id": "bold", "reason": "no_font_info"},))
    run = result.report.run
    assert run.options["join_lines"] is False
    assert run.options["bold"] is True
    assert run.rules_enabled == ("page_numbers",)
    assert run.rules_skipped[0]["rule_id"] == "bold"


def test_patch_construction_rejects_nonsense() -> None:
    ir = _ir()
    block = _block(ir, "17")
    with pytest.raises(PatchError):
        Patch.propose(patch_id="1", rule_id="page_numbers", block=block,
                      operation="delete_block")           # patch_id 不合规
    with pytest.raises(PatchError):
        Patch.propose(patch_id="p0001", rule_id="not_a_rule", block=block,
                      operation="delete_block")
    with pytest.raises(PatchError):
        Patch(patch_id="p0001", rule_id="page_numbers", target_block_id=block.id,
              operation="delete_block", before=BlockState.of(block),
              after=AfterState(text="不该有 after"))       # 删除不得带 after
    with pytest.raises(PatchError):
        Patch(patch_id="p0001", rule_id="headings", target_block_id=block.id,
              operation="set_level", before=BlockState.of(block),
              after=AfterState(text="改了正文", level=2))   # 改层级不得改正文


# ---------------------------------------------------------------- set_prefix(P5)
#
# 文档级操作:改 ``meta.prefix``。契约只有三条,但每条都要挡住一类事故:
#   - 目标必须是 ``book@prefix``(别拿块级流程去改一个不存在的块);
#   - ``after`` 必给,且必须真的改变内容(否则是空操作);
#   - 不带 ``trailer``/``level``(文首空白没有块间空行与层级可谈)。

def test_set_prefix_requires_the_document_target() -> None:
    ir = _ir()
    block = ir.editable_blocks[0]
    with pytest.raises(PatchError):
        Patch(patch_id="p0001", rule_id="blank_lines", target_block_id=block.id,
              operation="set_prefix", before=BlockState(text="\n\n\n"),
              after=AfterState(text=""))


def test_document_target_is_reserved_for_set_prefix() -> None:
    ir = _ir()
    block = ir.editable_blocks[0]
    with pytest.raises(PatchError):
        Patch(patch_id="p0001", rule_id="blank_lines",
              target_block_id="book@prefix", operation="replace_text",
              before=BlockState.of(block), after=AfterState(text=block.text + "。" ))


def test_set_prefix_must_change_something() -> None:
    with pytest.raises(PatchError):
        Patch.propose_prefix(patch_id="p0001", rule_id="blank_lines",
                             before_text="\n\n", after_text="\n\n")


def test_set_prefix_must_not_carry_block_level_fields() -> None:
    with pytest.raises(PatchError):
        Patch(patch_id="p0001", rule_id="blank_lines", target_block_id="book@prefix",
              operation="set_prefix", before=BlockState(text="\n\n\n"),
              after=AfterState(text="", trailer="\n\n"))
    with pytest.raises(PatchError):
        Patch(patch_id="p0001", rule_id="blank_lines", target_block_id="book@prefix",
              operation="set_prefix", before=BlockState(text="\n\n\n"),
              after=AfterState(text="", level=1))


def test_set_prefix_patch_survives_serialization() -> None:
    patch = Patch.propose_prefix(patch_id="p0001", rule_id="blank_lines",
                                 before_text="\n\n\n", after_text="")
    data = patch.to_dict()
    assert data["operation"] == "set_prefix"
    assert data["target_block_id"] == "book@prefix"
    assert data["before"]["text"] == "\n\n\n" and data["after"]["text"] == ""
    assert Patch.from_dict(data) == patch

    md = "\n\n\n" + "正文一段以句号结尾。\n"
    from clean import revert_patches as _revert

    ir = build_ir(md, backend="synthetic")
    result = apply_patches(ir, [Patch.from_dict(data)], started_at=0, finished_at=0)
    assert result.report.patches[0].status == "applied"
    assert result.md_after == "正文一段以句号结尾。\n"
    undone = _revert(result.ir, result.report, finished_at=0)
    assert undone.md_after == md
