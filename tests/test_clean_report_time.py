"""清理报告的审计时间必须真实、可测、可注入(P8 验收发现)。

P7 记录为 R-2:正式流程落盘的 ``clean-report.json`` 里
``run.started_at`` / ``run.finished_at`` **恒为 0** —— 报告说不出"这次跑的起止时刻",
既没法回答"这一份报告是哪一次运行产出的",也没法算出清理耗时。

根因有两处,缺一不可:

1. ``clean.rules.propose_rules`` 给 ``apply_patches`` 传的是写死的
   ``started_at=0, finished_at=0``。``apply_patches`` 的记时逻辑是
   ``t0 = int(clock()) if started_at is None else started_at`` —— 传了 0 就不是
   None,时钟**形同虚设**,连 ``clock`` 入参都救不回来;
2. ``clean.pipeline.merge_reports`` 从 ``reports[0] / reports[-1]`` 取时间,
   而 ``propose_rules`` 只为"确实提出了 Patch"的规则建报告 —— 一条都没命中时
   ``reports`` 是空的,合并结果退化成 ``0 / 0``。

修复:① ``propose_rules`` 不再钉死时间,改由 ``clock`` 取(默认 ``time.time``);
② ``merge_reports`` 接受调用方真实的 ``started_at`` / ``finished_at``;
③ ``clean_text`` / ``clean_book_md`` 暴露 ``clock`` 入参。

测试一律用**注入的假时钟**:断言"时间是这次跑出来的",且不需要真实等待。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from clean import apply_patches, clean_book_md, clean_text, revert_patches
from clean.patch import SourceFingerprint
from clean.report import CleanReport, RunInfo, read_report
from clean.rules import propose_dup_headings
from ir import build_ir

#: 一条规则都不会命中的纯文本(零命中分支:此前时间恒为 0)
CLEAN_MD = "# 书名\n\n一段很普通的中文正文,没有任何需要清理的东西。\n"
#: 会命中 dup_headings 的文本(有 Patch 的分支)
DIRTY_MD = "# 书名\n\n# 书名\n\n正文。\n"


class Clock:
    """可注入的假时钟:每次调用前进 ``step`` 秒,并记录取过的时刻。"""

    def __init__(self, t0: int = 1_700_000_000, step: int = 1) -> None:
        self.t = t0
        self.step = step
        self.values: list[int] = []

    def __call__(self) -> int:
        self.t += self.step
        self.values.append(self.t)
        return self.t


# ---------------------------------------------------------------- 成功路径

def test_clean_run_records_real_start_and_finish() -> None:
    """报告必须写出这次运行的起止时刻(而不是 0)。"""
    clock = Clock()
    outcome = clean_text(DIRTY_MD, backend="markdown", source="dup.md", clock=clock)

    run = outcome.report.run
    assert run.started_at == clock.values[0], f"起点不是本次运行的时刻: {run.started_at}"
    assert run.finished_at == clock.values[-1], f"终点不是本次运行的时刻: {run.finished_at}"
    assert run.finished_at >= run.started_at


def test_zero_patch_run_still_records_times() -> None:
    """一条规则都没命中时时间同样有效(合并报告的退化分支)。"""
    clock = Clock()
    outcome = clean_text(CLEAN_MD, backend="markdown", source="plain.md", clock=clock)

    assert outcome.applied == 0
    run = outcome.report.run
    assert run.started_at == clock.values[0], "零命中时起点退化成 0 了"
    assert run.finished_at == clock.values[-1], "零命中时终点退化成 0 了"


def test_default_clock_is_real_time_not_zero() -> None:
    """不注入时钟时(正式流程的默认路径)也必须拿到真实时刻。"""
    outcome = clean_text(CLEAN_MD, backend="markdown", source="plain.md")
    assert outcome.report.run.started_at > 0, "默认时钟没生效:时间戳仍是 0"


def test_clean_book_md_writes_times_to_disk(tmp_path: Path) -> None:
    """落盘报告里的时间是注入值 —— 正式流程写入的字段与内存里的一致。"""
    book = tmp_path / "book.md"
    book.write_text(DIRTY_MD, encoding="utf-8")
    clock = Clock(t0=1_700_000_000, step=7)

    clean_book_md(book, backend="markdown", source=str(book), clock=clock)

    report = read_report(tmp_path / "ir" / "clean-report.json")
    assert report.run.started_at == 1_700_000_007
    assert report.run.finished_at == clock.values[-1]
    assert report.run.finished_at > report.run.started_at


# ---------------------------------------------------------------- 失败路径

def test_failed_patch_run_still_records_times() -> None:
    """失败路径:Patch 被判 failed 时,报告的时间照样有效(不能退化成 0)。"""
    ir = build_ir(DIRTY_MD, source="dup.md", backend="markdown")
    proposal = propose_dup_headings(ir, first_id=1)
    assert proposal.patches, "夹具前提:这条文本必须能提出 Patch"
    # 换成"属于另一次输入"的指纹 → 每条 Patch 都会以 source_mismatch 失败
    foreign = tuple(
        replace(p, source_fingerprint=SourceFingerprint(
            book_md_sha256="0" * 64, ir_schema=ir.meta.schema,
            backend="other-backend", variant="other"))
        for p in proposal.patches
    )
    clock = Clock()
    result = apply_patches(ir, foreign, clock=clock)

    assert result.report.failed, "夹具前提:这些 Patch 必须判失败"
    assert result.report.run.started_at == clock.values[0]
    assert result.report.run.finished_at == clock.values[-1]


def test_revert_inherits_start_and_records_finish() -> None:
    """撤销报告:起点沿用被撤销那次运行的起点,终点是撤销的时刻。"""
    clock = Clock()
    outcome = clean_text(DIRTY_MD, backend="markdown", source="dup.md", clock=clock)
    assert outcome.applied >= 1

    revert_clock = Clock(t0=1_800_000_000, step=3)
    undone = revert_patches(outcome.ir_after, outcome.report, clock=revert_clock)

    assert undone.report.run.started_at == outcome.report.run.started_at
    assert undone.report.run.finished_at == revert_clock.values[-1]
    assert undone.report.run.finished_at > undone.report.run.started_at


# ---------------------------------------------------------------- 确定性与兼容

def test_same_clock_gives_byte_identical_report() -> None:
    """同一个假时钟重放 → 报告逐字节相同(不依赖真实等待也能证明确定性)。"""
    first = clean_text(DIRTY_MD, backend="markdown", source="dup.md",
                       clock=Clock()).report.to_json()
    second = clean_text(DIRTY_MD, backend="markdown", source="dup.md",
                        clock=Clock()).report.to_json()
    assert first == second


def test_deterministic_view_still_ignores_times() -> None:
    """时刻不同不影响确定性视图(差分测试的前提仍然成立)。"""
    early = clean_text(DIRTY_MD, backend="markdown", source="dup.md",
                       clock=Clock(t0=1_700_000_000)).report
    late = clean_text(DIRTY_MD, backend="markdown", source="dup.md",
                      clock=Clock(t0=1_900_000_000)).report
    assert early.run.started_at != late.run.started_at
    assert early.deterministic_view() == late.deterministic_view()


def test_legacy_report_with_zero_times_still_reads() -> None:
    """旧报告(started_at/finished_at 为 0)仍可正常读出,不会因为修时间而读不了。"""
    legacy = {
        "schema_version": 1,
        "engine": "ir",
        "run": {"started_at": 0, "finished_at": 0, "backend": "mineru",
                "source_md_sha256": "abc", "ir_schema": 1},
        "patches": [], "stats": {}, "verification": {}, "issues": [],
    }
    raw = json.dumps(legacy, ensure_ascii=False)
    report = CleanReport.from_dict(json.loads(raw))

    assert isinstance(report.run, RunInfo)
    assert report.run.started_at == 0 and report.run.finished_at == 0
    assert report.run.backend == "mineru"
    # 缺键的旧报告也不能炸
    assert RunInfo.from_dict({}).started_at == 0


def test_serialization_roundtrip_keeps_times() -> None:
    """序列化 → 反序列化后时间不丢(报告的对外契约没有变窄)。"""
    outcome = clean_text(DIRTY_MD, backend="markdown", source="dup.md", clock=Clock())
    again = CleanReport.from_dict(json.loads(outcome.report.to_json()))
    assert again.run.started_at == outcome.report.run.started_at
    assert again.run.finished_at == outcome.report.run.finished_at
