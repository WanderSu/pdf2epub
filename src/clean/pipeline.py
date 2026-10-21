"""正式清理管线:Markdown → IR → 规则 → Patch → Markdown(P6 的唯一入口)。

调用方(``batch``)只调 :func:`clean_book_md`,不需要知道 IR 的存在。中间发生的一切
都落在 ``<work>/ir/`` 里,可以事后逐条复核:

```
work/<书>/ir/
  meta.json          schema / backend / producer(谁产出的这份 md)/ 页数 / 统计
  blocks.jsonl       可编辑块 + 证据块(改动**后**的状态)
  links.jsonl        块 ↔ 证据的对齐关系
  clean-report.json  每条 Patch 的 before/after/证据/状态 + 统计 + 自检结论
  clean.diff         同上,人眼版本
```

四条硬约束:

1. **不静默降级**:IR 构造失败、规则未登记、Patch 应用后复核不过 —— 一律抛
   :class:`CleanEngineError`,由调用方把该文件判失败。**没有**"出错就走旧引擎"这条路
   (旧引擎在 P6 已删除);出错的正确处置是把错误暴露出来。
2. **不改原文**:清洗在内存里做,全部成功后才写回 ``book.md``。失败时磁盘上的文件
   一个字节不动。
3. **可撤销**:报告里的 ``before``/``after`` 足以把所有改动逆序撤回
   (``clean.revert_patches``),撤销前还要过 ``after.sha256`` 校验。
4. **页账不猜**:页归属来自证据层;没有证据层就如实记 ``undecided``,
   跨块删除降级为建议,绝不为凑覆盖率补页号。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from clean.engine import build_verification
from clean.options import CleanOptions, resolve_options
from clean.patch import Patch
from clean.policy import POLICY, auto_apply_rules, enabled_rules
from clean.report import (DIFF_FILE, REPORT_FILE, CleanReport, RunInfo,
                          build_stats, normalize_options, write_report)
from clean.rules import check_image_refs, propose_rules
from ir.model import UNKNOWN, BookIR, IRError, Producer, text_sha256
from ir.parse import build_ir
from ir.render import render_ir
from ir.store import dump_ir, ir_dir


class CleanEngineError(RuntimeError):
    """清理失败(IR 构造/Patch 应用/复核)。**必须**上报,不得回退到别的引擎。"""


@dataclass(frozen=True)
class CleanOutcome:
    """一次清理的完整结果(调用方只关心 ``md_after`` 与 ``report``)。"""

    md_before: str
    md_after: str
    ir_before: BookIR
    ir_after: BookIR
    report: CleanReport
    issues: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.md_after != self.md_before

    @property
    def applied(self) -> int:
        return len(self.report.applied)

    @property
    def suggested(self) -> int:
        return len(self.report.by_status("suggested"))

    @property
    def failed(self) -> int:
        return len(self.report.failed)

    def summary(self) -> str:
        """一行摘要(日志/事件用)。"""
        pages = self.report.verification.pages
        cover = ""
        if pages.get("expected"):
            cover = f", 页覆盖 {pages.get('covered', 0)}/{pages['expected']}"
        return (f"applied {self.applied} / suggested {self.suggested} / "
                f"failed {self.failed}, 字符 {len(self.md_before)}→{len(self.md_after)}"
                f"{cover}")


# ---------------------------------------------------------------- 输入归一

def normalize_newlines(md: str) -> tuple[str, str | None]:
    """CRLF/CR → LF;返回 ``(文本, 说明)``,没改就说明为 None。

    为什么要归一:下游规则与 pandoc 都按 ``\\n`` 工作,混着 ``\\r`` 会让
    "行尾"有两个定义(逐字节 diff 里表现为整篇重写)。这是**编码层**归一,
    不是内容改写 —— 但仍然是字节变化,所以要在报告 issues 里留一句。
    """
    if "\r" not in md:
        return md, None
    fixed = md.replace("\r\n", "\n").replace("\r", "\n")
    return fixed, f"输入换行归一:CRLF/CR → LF({len(md)} → {len(fixed)} 字符)"


# ---------------------------------------------------------------- 报告合并

def merge_reports(reports: list[CleanReport], *, ir_after: BookIR,
                  md_before: str, md_after: str, blocks_before: int,
                  issues: tuple[str, ...] = (),
                  options: dict | None = None,
                  rule_ids: tuple[str, ...] | None = None,
                  started_at: int | None = None,
                  finished_at: int | None = None) -> CleanReport:
    """把"每条规则一份报告"合并成**一份**落盘报告。

    为什么合并:``clean-report.json`` 是这本书的对外契约,读端不该为了看全貌去
    glob 九个文件。Patch 顺序保持执行顺序(它同时是撤销所依赖的顺序)。

    下面四项是调用方的**真实入参**,优先于从 ``reports`` 里取:

    ``options`` / ``rule_ids``:

    ``propose_rules`` 只为"确实提出了 Patch"的规则建报告,一条都没命中时
    ``reports`` 是空的 —— 此时若拿 ``reports[0]`` 当依据,报告会自称
    ``rules_enabled: []`` 且 ``options`` 变成默认值,与它自己的 ``engine:"ir"``
    和真实开关互相矛盾(命令行关掉某项、又恰好没命中时,报告反而说全开着)。
    报告要能自证"这次到底开着哪些开关跑了哪些规则",所以这两项以入参为准。

    ``started_at`` / ``finished_at``:同理 —— 一条规则都没命中时没有任何 per-rule
    报告可供取值,合并结果就会变成 ``0/0``(P8:报告说不出"这次跑的起止时刻")。
    调用方(:func:`clean_text`)自己记了整段清理的起止,以它的为准;没传时才退回
    ``reports[0]/[-1]``,再没有才记 0。
    """
    patches: tuple[Patch, ...] = tuple(p for r in reports for p in r.patches)
    first, last = (reports[0], reports[-1]) if reports else (None, None)
    run = RunInfo(
        started_at=started_at if started_at is not None
        else (first.run.started_at if first else 0),
        finished_at=finished_at if finished_at is not None
        else (last.run.finished_at if last else 0),
        backend=ir_after.meta.backend or UNKNOWN,
        source_md_sha256=text_sha256(md_before),
        ir_schema=ir_after.meta.schema,
        options=normalize_options(options if options is not None
                                  else (first.run.options if first else None)),
        rules_enabled=(tuple(rule_ids) if rule_ids is not None
                       else (first.run.rules_enabled if first else ())),
        rules_skipped=first.run.rules_skipped if first else (),
    )
    readonly = sum(r.verification.readonly_violations for r in reports)
    source_ok = all(r.verification.source_ok for r in reports)
    return CleanReport(
        engine="ir",
        run=run,
        patches=patches,
        stats=build_stats(patches, chars_before=len(md_before),
                          chars_after=len(md_after), blocks_before=blocks_before,
                          blocks_after=len(ir_after.editable_blocks)),
        verification=build_verification(ir_after, md_after=md_after,
                                        readonly=readonly, source_ok=source_ok),
        issues=issues,
    )


# ---------------------------------------------------------------- 主流程

def clean_text(md: str, *, backend: str = UNKNOWN, source: str = "",
               evidence: list | None = None, total_pages: int = 0,
               variant: str = "original", part_id: str = "book",
               producer: Producer | None = None,
               options: CleanOptions | None = None,
               images_dir: str | Path | None = None,
               write_reports_to: str | Path | None = None,
               clock=None) -> CleanOutcome:
    """清理一段 Markdown,返回结果(不落盘,除非给了 ``write_reports_to``)。

    ``producer``:这份 md 是**谁**产出的。调用方必须显式给(``Producer.from_backend``);
    不给就记 ``unknown`` —— 不猜。页账与回放都靠它归属"哪次 OCR 的产物"。
    ``evidence``:后端给的 ``content_list``(只读证据层)。没有它时页账记
    ``undecided``,跨块拼接的删除降级为建议(不猜、不补页号)。
    ``clock``:取时刻的来源(默认 ``time.time``)。注入它是为了让"报告里的时间"
    可测 —— 测试要能不靠真实等待就断言起止时刻有效。
    """
    clock = clock or time.time
    options = options or CleanOptions()
    started_at = int(clock())          # 整段清理的起点(含 IR 构造)
    md, notice = normalize_newlines(md)
    issues: list[str] = []
    if notice:
        issues.append(notice)

    try:
        ir = build_ir(
            md, source=source, backend=backend or UNKNOWN,
            total_pages=total_pages, variant=variant or "original",
            evidence=evidence, part_id=part_id, producer=producer,
        )
    except IRError as exc:
        raise CleanEngineError(f"IR 构造失败({source or '未命名输入'}): {exc}") from exc

    rule_ids = enabled_rules(options)
    try:
        ir_after, reports = propose_rules(
            ir, rule_ids, allow_rules=auto_apply_rules(options),
            options=options.to_dict(), clock=clock,
        )
    except IRError as exc:
        raise CleanEngineError(f"清理失败({source or '未命名输入'}): {exc}") from exc

    # 规则说明(为什么没拼/为什么删了哪些行)进 issues:它们是"观察",
    # 不产生字节改动,混进 patches 会让"改了几处"这个数字失去意义。
    for report in reports:
        for patch in report.patches:
            for note in patch.validation.notes:
                if note and ("降级" in note or "已知差异" in note):
                    issues.append(f"[{patch.rule_id}] {note}")

    md_after = render_ir(ir_after)
    if options.images:
        issues.extend(check_image_refs(md_after, images_dir))

    merged = merge_reports(reports, ir_after=ir_after, md_before=md,
                           md_after=md_after, blocks_before=len(ir.editable_blocks),
                           issues=tuple(dict.fromkeys(issues)),
                           options=options.to_dict(), rule_ids=rule_ids,
                           started_at=started_at, finished_at=int(clock()))
    outcome = CleanOutcome(md_before=md, md_after=md_after, ir_before=ir,
                           ir_after=ir_after, report=merged, issues=merged.issues)
    if write_reports_to is not None:
        write_artifacts(outcome, write_reports_to)
    return outcome


def write_artifacts(outcome: CleanOutcome, work_dir: str | Path) -> Path:
    """把 IR 与报告写到 ``<work_dir>/ir/``(IR 本身也落盘,便于事后复核)。"""
    out = ir_dir(work_dir)
    dump_ir(outcome.ir_after, work_dir)
    write_report(outcome.report, out / REPORT_FILE)
    (out / DIFF_FILE).write_text(_diff(outcome.md_before, outcome.md_after),
                                 encoding="utf-8")
    return out


def _diff(before: str, after: str) -> str:
    from clean.diff import render_unified_diff

    return render_unified_diff(before, after)


def clean_book_md(book_md: str | Path, *, backend: str = UNKNOWN,
                  evidence: list | None = None, total_pages: int = 0,
                  variant: str = "original", source: str = "",
                  producer: Producer | None = None,
                  options: CleanOptions | None = None,
                  images_dir: str | Path | None = None,
                  write_reports: bool = True, clock=None) -> CleanOutcome:
    """就地清理 ``work/<书>/book.md``(正式转换流程的入口)。

    读取 → 清理 → **全部成功才写回**。写回前不碰磁盘上的任何字节:中途抛
    :class:`CleanEngineError` 时 book.md 保持原样,批处理会把该文件判失败,
    而不是留下"清理了一半"的产物。

    ``clock`` 透传给 :func:`clean_text`(可注入时钟,便于断言报告时间戳)。
    """
    path = Path(book_md)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CleanEngineError(f"读取 {path} 失败: {exc}") from exc

    outcome = clean_text(
        raw, backend=backend, source=source or str(path), evidence=evidence,
        total_pages=total_pages, variant=variant, producer=producer, options=options,
        images_dir=images_dir if images_dir is not None else path.parent / "images",
        write_reports_to=path.parent if write_reports else None, clock=clock,
    )
    if outcome.changed:
        try:
            path.write_text(outcome.md_after, encoding="utf-8")
        except OSError as exc:
            raise CleanEngineError(f"写回 {path} 失败: {exc}") from exc
    return outcome


def policy_table() -> list[dict]:
    """策略表的可序列化视图(报告/调试/文档用)。"""
    return [{"rule_id": p.rule_id, "confidence": p.confidence,
             "auto_apply": p.auto_apply, "why": p.why} for p in POLICY]


def resolve_or_default(config: dict | None = None,
                       disable=None) -> CleanOptions:
    """配置 → CleanOptions(转发 ``clean.options.resolve_options``,方便调用方只 import 一个名字)。"""
    return resolve_options(config, disable)


__all__ = [
    "CleanEngineError", "CleanOutcome", "clean_book_md", "clean_text",
    "merge_reports", "normalize_newlines", "policy_table", "write_artifacts",
]
