"""``clean-report.json`` schema v1(阶段 B §E.7)。

这一组测试守的是「报告能不能被信任」,不是「清理效果好不好」:

  - 字段集合与顺序冻结(**加字段是兼容变更,改字段名/删字段不是**);
  - 报告的 ``before.sha256`` 与 ``before.text`` 必须自洽(否则 before 可伪造);
  - 未知枚举读成 ``unknown`` 且永不执行;
  - ``schema_version`` 不认识就拒绝,不猜;
  - 同输入两次运行除时间戳外逐字节相同(差分测试的前提)。

夹具全是合成文本,不含任何真实书籍正文。
"""
from __future__ import annotations

import json

import pytest

from clean import (CleanReport, Patch, PatchError, ReportVersionError,
                   SCHEMA_VERSION, apply_patches, default_options, read_report,
                   write_report)
from clean.patch import BlockState, Evidence
from ir import build_ir, text_sha256

MD = """\
# 第一章 起头

正文第一段,句尾没有标点。

17

第二段正文,用来做替换目标。
"""

#: §E.7 表格里"必需"的字段,一个都不能少(P3 加法:depends_on/after.trailer
#: 为可选字段,旧读端可忽略,schema_version 不变)
REQUIRED_PATCH_FIELDS = (
    "patch_id", "rule_id", "target_block_id", "operation", "before", "after",
    "evidence", "confidence", "risk", "status", "skip_reason",
    "source_fingerprint", "depends_on", "validation",
)


def _ir(md: str = MD):
    return build_ir(md, source="synthetic.md", backend="mineru", total_pages=2)


def _block(ir, needle: str):
    return next(b for b in ir.editable_blocks if needle in b.text)


def _patches(ir):
    page_no = _block(ir, "17")
    body = _block(ir, "第二段")
    return [
        Patch.propose(patch_id="p0001", rule_id="page_numbers", block=page_no,
                      operation="delete_block",
                      evidence=Evidence(kind="page_number", matched="17",
                                        source="evidence_block")),
        Patch.propose(patch_id="p0002", rule_id="cjk_spaces", block=body,
                      operation="replace_text",
                      after_text="第二段正文，用来做替换目标。",
                      evidence=Evidence(kind="text", matched="正文,", source="md_block")),
    ]


def _report(ir=None):
    ir = ir or _ir()
    return apply_patches(ir, _patches(ir), started_at=0, finished_at=0).report


def test_top_level_envelope_keys_are_frozen() -> None:
    data = _report().to_dict()
    # P6 新增 ``issues``:规则观察(缺证据降级、已知差异、换行归一)要与 Patch
    # 分列 —— 混进 patches 会让「改了几处」这个数字失去意义。
    assert list(data) == [
        "schema_version", "engine", "run", "patches", "stats", "verification",
        "issues",
    ]
    assert data["schema_version"] == SCHEMA_VERSION == 1
    assert data["engine"] == "ir"
    assert list(data["run"]) == [
        "started_at", "finished_at", "backend", "source_md_sha256", "ir_schema",
        "options", "rules_enabled", "rules_skipped",
    ]
    assert list(data["stats"]) == [
        "by_rule", "by_status", "chars_before", "chars_after",
        "blocks_before", "blocks_after",
    ]
    assert list(data["verification"]) == [
        "roundtrip_ok", "pages", "page_attribution_missing",
        "readonly_violations", "source_ok",
    ]


def test_patch_carries_every_required_field_in_order() -> None:
    patches = _report().patches
    delete, replace = patches[0], patches[1]
    data = delete.to_dict()
    assert tuple(data) == REQUIRED_PATCH_FIELDS
    assert data["after"] is None, "delete_block 的 after 必须是 null"
    assert tuple(data["before"]) == (
        "text", "sha256", "md_span", "level", "order", "trailer", "kind",
        "kind_confidence", "page_idx", "page_idx_source",
    )
    assert tuple(replace.to_dict()["after"]) == ("text", "sha256", "level",
                                            "trailer")  # P3 加法,可选
    for name in ("kind", "page_idx", "bbox", "bbox_space", "text_level",
                 "matched", "source"):
        assert name in data["evidence"], f"evidence 缺 {name}"
    for name in ("before_ok", "after_ok", "kind_confidence_ok", "applied_at", "notes"):
        assert name in data["validation"], f"validation 缺 {name}"


def test_patch_dict_roundtrips_without_losing_any_field() -> None:
    patch = _report().patches[0]
    once = patch.to_dict()
    twice = Patch.from_dict(json.loads(json.dumps(once, ensure_ascii=False))).to_dict()
    assert twice == once
    assert json.dumps(twice, ensure_ascii=False) == json.dumps(once, ensure_ascii=False)


def test_before_sha_must_match_before_text() -> None:
    with pytest.raises(PatchError):
        BlockState(text="正文", sha256=text_sha256("另一段正文"))
    ok = BlockState(text="正文")
    assert ok.sha256 == text_sha256("正文")


def test_missing_required_field_fails_fast() -> None:
    with pytest.raises(PatchError):
        Patch.from_dict({"patch_id": "", "rule_id": "page_numbers"})
    with pytest.raises(PatchError):
        Patch.from_dict({})


def test_unknown_enums_read_as_unknown_and_never_execute() -> None:
    ir = _ir()
    patch = _patches(ir)[1]
    raw = patch.to_dict()
    raw["status"] = "自动通过"            # 未来版本的新状态
    raw["rule_id"] = "future_rule"
    read = Patch.from_dict(raw)
    assert read.status == "unknown"
    assert read.rule_id == "unknown"

    result = apply_patches(ir, [read], started_at=0, finished_at=0)
    assert result.md_after == MD                     # 一个字符都没动
    assert result.patches[0].status == "unknown"


def test_unknown_schema_version_is_refused() -> None:
    data = _report().to_dict()
    data["schema_version"] = 2
    with pytest.raises(ReportVersionError):
        CleanReport.from_dict(data)


def test_report_written_and_read_back_is_identical(tmp_path) -> None:
    report = _report()
    path = write_report(report, tmp_path / "clean-report.json")
    again = read_report(path)
    assert again.to_json() == report.to_json()
    assert again.to_dict() == report.to_dict()


def test_same_input_produces_identical_bytes(tmp_path) -> None:
    ir = _ir()
    first = apply_patches(ir, _patches(ir), started_at=0, finished_at=0).report
    second = apply_patches(ir, _patches(ir), started_at=0, finished_at=0).report
    assert first.to_json() == second.to_json()

    a = write_report(first, tmp_path / "a.json")
    b = write_report(second, tmp_path / "b.json")
    assert a.read_bytes() == b.read_bytes()


def test_deterministic_view_ignores_timestamps() -> None:
    ir = _ir()
    early = apply_patches(ir, _patches(ir), started_at=1, finished_at=2).report
    late = apply_patches(ir, _patches(ir), started_at=99, finished_at=100).report
    assert early.to_json() != late.to_json()
    assert early.deterministic_view() == late.deterministic_view()


def test_default_options_are_the_nine_clean_keys() -> None:
    from clean.options import CLEAN_KEYS

    options = default_options()
    assert set(options) == set(CLEAN_KEYS)
    assert len(options) == 9
    assert options["bold"] is False            # 与 cleaner.CleanOptions 默认值一致
    assert list(_report().run.options) == list(CLEAN_KEYS)


def test_stats_keep_enum_order() -> None:
    stats = _report().stats
    assert list(stats.by_status) == ["applied"]            # 只列出现过的
    assert list(stats.by_rule) == ["page_numbers", "cjk_spaces"]
    assert stats.by_status["applied"] == 2
    assert stats.chars_after < stats.chars_before
    assert stats.blocks_after == stats.blocks_before - 1   # 删了一块
