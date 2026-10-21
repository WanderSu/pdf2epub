"""Patch 应用与撤销(阶段 B §E.7 的双校验规则落地)。

这个模块只有两条职责,且都不涉及"该不该改"的判断:

1. **按报告执行**:每条 Patch 应用前比对 ``before.sha256``,撤销前比对
   ``after.sha256``;对不上就记失败,**一个字符都不动**。
2. **把结果写成可复查的账**:每条 Patch 的最终 ``status``、``validation``、
   以及整本书的 ``stats`` / ``verification``。

判断"该不该改"是规则层(P3)的事。引擎不知道 `join_lines` 的语义,也不需要知道 ——
它只认 `operation` 与 `evidence.sufficient`。这条分工是刻意的:引擎一旦开始懂规则,
"为什么改了"就再也说不清。

失败路径都落在状态里,不抛异常(除构造期的非法 Patch):

    candidate  规则给的候选,尚未处理
    applied    已改,且改后哈希校验通过
    suggested  有候选但证据不足 / 置信度低,只记建议不动正文
    skipped    无需动作(已是目标状态、已撤销过、正文不变)
    rejected   目标不可改写(证据块)
    failed     校验失败(目标不存在、before/after 不符、输入指纹不符)

``merge_blocks`` 目前**一律拒绝**(``operation_mismatch``):合并会把两块变成一块,
而一条 Patch 只记得住一个 ``before`` 快照,撤销时被吸收的那块没有地方可还原。
合并请由规则层拆成 ``replace_text`` + ``delete_block`` 两条 Patch —— 两条各自
可审计、可撤销,而不是留一个"能改不能还原"的操作。

撤销**必须**从报告读,不能从 ``clean.diff`` 猜 —— diff 不知道块 id,
更不知道"这条到底改没改成功"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from clean.diff import render_unified_diff
from clean.patch import (AfterState, BlockState, Patch, PatchError,
                         SourceFingerprint, Validation)
from clean.report import (DIFF_FILE, REPORT_FILE, CleanReport, RunInfo,
                          Verification, build_stats, normalize_options,
                          write_report)
from ir.model import (KINDS, PAGE_SOURCES, Block, BookIR, IRError, Provenance,
                      text_sha256)
from ir.pages import (GAP_STATES, covered_pages, page_gaps, uncovered_pages)
from ir.parse import build_ir
from ir.render import render_ir
from ir.store import ir_dir

#: 已经表态、不需要引擎动手的状态
_DECIDED = frozenset({"suggested", "skipped", "rejected", "failed", "unknown"})

#: 「动正文」的操作 —— 证据不足时只产建议
_DESTRUCTIVE = frozenset({"delete_block", "merge_blocks"})


@dataclass(frozen=True)
class ApplyResult:
    """一次应用的完整结果:改后的 IR + 报告 + 前后的 Markdown(供 review)。"""

    ir: BookIR
    report: CleanReport
    md_before: str
    md_after: str
    diff: str

    @property
    def patches(self) -> tuple[Patch, ...]:
        return self.report.patches

    @property
    def changed(self) -> bool:
        return self.md_before != self.md_after

    def write(self, work_dir: str | Path) -> Path:
        """写 ``ir/clean-report.json`` 与 ``ir/clean.diff``(后者仅供人看)。"""
        out = ir_dir(work_dir)
        out.mkdir(parents=True, exist_ok=True)
        write_report(self.report, out / REPORT_FILE)
        (out / DIFF_FILE).write_text(self.diff, encoding="utf-8")
        return out


# ---------------------------------------------------------------- 状态助手

def _note(notes: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(n) for n in notes)


def _failed(patch: Patch, reason: str, *notes: str, before_ok: bool = False) -> Patch:
    return patch.with_outcome(
        "failed", skip_reason=reason,
        validation=Validation(before_ok=before_ok, after_ok=False,
                              notes=_note(notes)),
    )


def _skipped(patch: Patch, reason: str | None, *notes: str) -> Patch:
    return patch.with_outcome(
        "skipped", skip_reason=reason,
        validation=Validation(before_ok=True, after_ok=True, notes=_note(notes)),
    )


def _suggested(patch: Patch, reason: str, *notes: str) -> Patch:
    return patch.with_outcome(
        "suggested", skip_reason=reason,
        validation=Validation(before_ok=True, after_ok=False, notes=_note(notes)),
    )


def _rejected(patch: Patch, reason: str, *notes: str) -> Patch:
    return patch.with_outcome(
        "rejected", skip_reason=reason,
        validation=Validation(before_ok=True, after_ok=False,
                              kind_confidence_ok=False, notes=_note(notes)),
    )


# ---------------------------------------------------------------- 内部工具

def _stamp_before(recorded: BlockState, block: Block, notes: list[str]) -> BlockState:
    """把 ``before`` 换成**当前块的真实快照**。

    报告里记的是"检测时以为的块";真正落盘必须是"动手时的块"。两者不一致时
    记一条 note 而不是失败:正文哈希已经由 ``before.sha256`` 卡住了,位置/空行
    这类元数据以实际值为准,撤销才有足够信息把块原样放回去。
    """
    actual = BlockState.of(block)
    for key in ("md_span", "level", "order", "trailer", "kind", "kind_confidence",
                "page_idx", "page_idx_source"):
        given = getattr(recorded, key)
        truth = getattr(actual, key)
        if given in (None, ""):
            continue
        if given != truth:
            notes.append(f"before.{key} 记录值 {given!r} 与当前块 {truth!r} 不同,以实际值为准")
    return actual


def _merge_not_supported(patch: Patch) -> Patch:
    """``merge_blocks`` 暂时拒绝执行,并说清楚替代写法。

    合并会把**两块**变成一块,而一条 Patch 只能记一个 ``before`` 快照 ——
    被吸收的那一块(id、块间空行、页归属)没有任何字段可放。硬做下去的结果是
    "能合并、撤不回来":撤销时那一块凭空消失,而这正是本次重构要消灭的事故类型。

    所以合并必须由规则层拆成两条 Patch 表达(``replace_text`` + ``delete_block``),
    两条各自可审计、可撤销。这里显式失败,而不是留一个撤不回来的操作。
    """
    return _failed(
        patch, "operation_mismatch",
        "merge_blocks 不可撤销(单条 Patch 记不下被吸收的块),"
        "请改用 replace_text + delete_block 两条 Patch 表达合并",
    )


def _refresh_meta(ir: BookIR) -> BookIR:
    """改写之后 ``counts`` 会失真;把它按当前内容重算(尤其是 ``md_sha256``)。"""
    md = render_ir(ir)
    counts = dict(ir.meta.counts)
    counts.update({
        "blocks": len(ir.blocks),
        "editable": len(ir.editable_blocks),
        "evidence": len(ir.evidence_blocks),
        "links": len(ir.links),
        "page_attribution_missing": ir.page_attribution_missing,
        "md_sha256": text_sha256(md),
    })
    return replace(ir, meta=replace(ir.meta, counts=counts))


def _apply_one(ir: BookIR, patch: Patch, block: Block, applied_at: int) -> tuple[BookIR, Patch]:
    """执行一条候选 Patch;返回 (新 IR, 盖过章的新 Patch)。"""
    notes: list[str] = []
    before = _stamp_before(patch.before, block, notes)
    op = patch.operation

    if op == "noop":
        after = AfterState(text=before.text, level=before.level)
        return ir, patch.with_outcome(
            "applied", validation=Validation(True, True, True, applied_at, _note(notes)),
            before=before, after=after,
        )

    if op == "delete_block":
        # after 必须是 None(构造期已挡);删除记录里保留 before 的完整快照
        return ir.remove_block(block.id), patch.with_outcome(
            "applied", validation=Validation(True, True, True, applied_at, _note(notes)),
            before=before,
        )

    after = patch.after
    if after is None:                                  # pragma: no cover - 构造期已挡
        raise PatchError(f"{op} 必须给出 after")

    if op == "replace_text":
        trailer_now = str(block.extra.get("trailer", ""))
        # trailer 语义:None = 不改;否则必须与当前不同(否则是空操作)
        new_trailer = trailer_now if after.trailer is None else after.trailer
        # P6:``headings`` 要在同一条 Patch 里同时改文本形态与层级(``#`` 的个数)。
        # 拆成两条会让后一条的 before 被前一条改掉,链式校验当场失效。
        want_level = block.level if after.level is None else after.level
        if after.text == before.text and new_trailer == trailer_now \
                and want_level == block.level:
            raise PatchError("replace_text 的 after 与 before 完全相同")
        new_ir = ir.rewrite(block.id, after.text)
        if after.trailer is not None and after.trailer != trailer_now:
            target = replace(new_ir.block(block.id),
                             extra={**block.extra, "trailer": after.trailer})
            new_ir = new_ir.replace_block(block.id, target)
            notes.append(f"块间空行 {trailer_now!r} → {after.trailer!r}")
        if want_level != block.level:
            new_ir = new_ir.replace_block(
                block.id, replace(new_ir.block(block.id), level=want_level))
            notes.append(f"层级 H{block.level} → H{want_level}")
        after = replace(after, level=want_level)
    elif op == "set_level":
        if after.level is None:
            raise PatchError("set_level 缺少 after.level")
        if after.level == before.level:
            raise PatchError(f"set_level 未改变层级(仍是 H{before.level})")
        new_ir = ir.replace_block(block.id, replace(block, level=after.level))
        after = replace(after, level=after.level)
    else:                                              # pragma: no cover - 构造期已挡
        raise PatchError(f"不支持的 operation: {op}")

    new_block = new_ir.block(block.id)
    after_ok = _text_of(new_ir, block.id) == after.sha256
    want_trailer = after.trailer if after.trailer is not None else before.trailer
    trailer_ok = str(new_block.extra.get("trailer", "")) == want_trailer
    level_ok = after.level is None or new_block.level == after.level
    if not after_ok:
        notes.append("改后哈希与 after.sha256 不符")
    if not trailer_ok:
        notes.append("改后块间空行与预期不符")
    if not level_ok:
        notes.append("改后层级与预期不符")
    after_ok = after_ok and trailer_ok and level_ok
    return new_ir, patch.with_outcome(
        "applied" if after_ok else "failed",
        skip_reason=None if after_ok else "after_mismatch",
        validation=Validation(True, after_ok, True, applied_at, _note(notes)),
        before=before, after=after,
    )


def _text_of(ir: BookIR, block_id: str) -> str | None:
    block = ir.block(block_id)
    return block.sha256 if block is not None else None


def _apply_prefix(ir: BookIR, patch: Patch, applied_at: int) -> tuple[BookIR, Patch]:
    """文档级操作:改 ``meta.prefix``(文首空白)。

    没有块可查,所以校验只能落在 ``prefix`` 原文上 —— 与块级 Patch 的
    ``before.sha256`` / ``after.sha256`` 是同一套判据,只是对象换成了文档头。
    """
    after = patch.after
    if after is None:                                  # pragma: no cover - 构造期已挡
        raise PatchError("set_prefix 必须给出 after")
    current = ir.meta.prefix
    if current == after.text:
        return ir, _skipped(patch, "already_applied", "文首空白已是 after 状态")
    if current != patch.before.text:
        return ir, _failed(
            patch, "before_mismatch",
            f"文首空白当前 {current!r} 与 before {patch.before.text!r} 不符,未修改",
        )
    new_ir = replace(ir, meta=replace(ir.meta, prefix=after.text))
    ok = new_ir.meta.prefix == after.text and \
        text_sha256(new_ir.meta.prefix) == after.sha256
    return new_ir, patch.with_outcome(
        "applied" if ok else "failed",
        skip_reason=None if ok else "after_mismatch",
        validation=Validation(True, ok, True, applied_at,
                             _note((f"文首空白 {current!r} → {after.text!r}",))),
    )


def _already_applied(block: Block, patch: Patch) -> bool:
    """目标块是否已经是 ``after`` 状态(重复应用要在改之前就拦住)。

    ``set_level`` 的特殊之处:正文没变,拿文本哈希判断"改没改"永远为真,只能比层级。
    ``noop`` 永远不算"已应用"—— 它本来就是"什么也没做",要留下 applied 记录。
    带 ``after.trailer`` 的 Patch:正文一致还不够,块间空行也必须是改后值。
    带 ``after.level`` 的 ``replace_text``(P6 的 headings):层级也要对得上。
    """
    if patch.operation == "noop":
        return False
    if patch.operation == "set_level":
        return patch.after is not None and block.level == patch.after.level
    if patch.after is None:
        return False
    if patch.after.trailer is not None \
            and str(block.extra.get("trailer", "")) != patch.after.trailer:
        return False
    if patch.after.level is not None and block.level != patch.after.level:
        return False
    return block.sha256 == patch.after.sha256


def _structural_self_evidence(patch: Patch, block: Block) -> bool:
    """「这一整块就是判断对象」的自证删除(P6)。

    为什么需要它:``page_numbers`` / ``running_heads`` / ``dup_headings`` /
    ``headings`` 要删的是**整块**,而这几条规则的判断依据恰好是块自身的形态
    (整行就是一个页码 / 整行就是重复页眉 / 整块就是空标题)。它们的证据既不是
    证据层链接、也没有 bbox —— 按 ``Evidence.sufficient`` 永远过不了,于是这些
    确定性的清理只能停在建议,而放开 ``sufficient`` 又会把"文本规则猜测删正文"
    一起放进来(历史上 13 处拼接有 12 处是这么误伤的)。

    所以判据收窄到四个条件同时成立,缺一不删:

    1. ``evidence.source == "md_block"``:证据就是这一块自己,不是别处的推断;
    2. ``evidence.scope == "whole"``:规则声明命中的是**整块**;
    3. ``evidence.matched == block.text.strip()``:由引擎核对(不是信规则的自述);
    4. ``confidence`` 是 ``explicit``/``derived``:判断是"数出来的/读出来的",
       不是启发式猜测。``heuristic`` 一律走 ``evidence.sufficient``,这一条不放行。
    """
    ev = patch.evidence
    if ev.source != "md_block" or ev.scope != "whole":
        return False
    if patch.confidence not in ("explicit", "derived"):
        return False
    return bool(ev.matched.strip()) and ev.matched.strip() == block.text.strip()


def _content_gone(ir: BookIR, patch: Patch) -> bool:
    """被删的那块是不是真的不在了(同正文 + 同块间空行的可编辑块一个都没有)。

    只看"块"这一级,不去全文搜字符串:正文可能被别处引用、可能出现在注释里,
    拿字符串是否出现来决定"删没删过"就是把模糊匹配引进来了。
    """
    text = patch.before.text
    trailer = patch.before.trailer
    return not any(
        b.editable and b.text == text and str(b.extra.get("trailer", "")) == trailer
        for b in ir.blocks
    )


def _roundtrip_ok(md: str) -> bool:
    """改完的文档还能不能被 IR 原样承载(切分→还原逐字节相等)。"""
    try:
        return render_ir(build_ir(md)) == md
    except IRError:
        return False


def build_verification(ir: BookIR, *, md_after: str, readonly: int,
                       source_ok: bool) -> Verification:
    # 页号口径:``page_idx`` 是 0-based,``ir.pages`` / 报告的 ``missing`` 是 1-based。
    # P4 之前这里拿两个集合直接做差,整套页号错位一格 → 明明每页都有内容却报
    # "全部页未覆盖"。换算交给 ir.pages,只做一次。
    gaps = page_gaps(ir)
    by_state = {state: [g.page for g in gaps if g.state == state]
                for state in GAP_STATES}
    return Verification(
        roundtrip_ok=_roundtrip_ok(md_after),
        pages={
            "expected": ir.meta.total_pages,
            "covered": len(covered_pages(ir)),
            # P5:blank 不再是恒空占位。它表示"证据层里这一页什么内容都没有"
            # (只有页码/页眉页脚或干脆没有条目)—— 这是**证据层判定**,不是渲染判定;
            # 判不了的那一类单独落在 no_text,不冒充 blank。
            "blank": by_state["blank"],
            "missing": list(uncovered_pages(ir)),
            # 下面三项是 P5 加法(读端可忽略),把 missing 拆成可处置的三类
            "unattributed": by_state["unattributed"],
            "no_text": by_state["no_text"],
            "undecided": by_state["unavailable"],
        },
        page_attribution_missing=ir.page_attribution_missing,
        readonly_violations=readonly,
        source_ok=source_ok,
    )


# ---------------------------------------------------------------- 应用

def apply_patches(ir: BookIR, patches: Sequence[Patch], *, engine: str = "ir",
                  options: dict | None = None,
                  rules_enabled: Iterable[str] = (),
                  rules_skipped: Iterable[dict] = (),
                  started_at: int | None = None, finished_at: int | None = None,
                  clock=time.time,
                  allow_heuristic_apply: bool = False,
                  allow_rules: Iterable[str] = ()) -> ApplyResult:
    """按给定顺序应用 Patch,返回新 IR 与完整报告。

    顺序即执行顺序(**不按 patch_id 重排**):同一块上的多条 Patch 若被重排,
    后一条的 ``before`` 就不再等于前一条的 ``after``,链式校验会当场失效。
    报告里的 ``patches`` 因此保持调用方给的顺序。

    ``allow_heuristic_apply``:默认 False —— ``confidence="heuristic"`` 的候选
    只产建议、不动正文(阶段 B 决策 5)。打开它只应出现在人工确认后的重跑。

    ``allow_rules``:按**规则**放行 heuristic 候选(P6)。正式流程用它而不是整批
    放行:``clean.policy`` 只把"判据已与旧引擎逐字节对齐、并在三本真实书上验收过"
    的规则标成可自动应用,其余仍在建议里等人工。两个开关的关系是"或"。
    """
    t0 = int(clock()) if started_at is None else started_at
    md_before = render_ir(ir)
    fingerprint = SourceFingerprint.of(ir)
    blocks_before = len(ir.editable_blocks)
    allowed = frozenset(allow_rules)

    current = ir
    records: list[Patch] = []
    last_after: dict[str, str] = {}
    seen_ids: set[str] = set()
    readonly = 0
    source_ok = True
    applied_at = t0

    for patch in patches:
        if patch.patch_id in seen_ids:
            records.append(_failed(patch, "ambiguous_target",
                                   f"patch_id 重复: {patch.patch_id}"))
            continue
        seen_ids.add(patch.patch_id)

        if patch.status in _DECIDED:
            records.append(patch)                       # 规则已表态/未知:不执行
            continue
        if "unknown" in (patch.rule_id, patch.operation):
            records.append(replace(
                patch, status="unknown",
                validation=Validation(notes=("未知 rule_id/operation,未执行",)),
            ))
            continue

        if patch.source_fingerprint is not None and not patch.source_fingerprint.matches(fingerprint):
            source_ok = False
            records.append(_failed(
                patch, "source_mismatch",
                "报告记录的是另一次输入(book_md_sha256/ir_schema/backend/variant 不符),"
                "拒绝应用",
            ))
            continue
        if patch.source_fingerprint is None:
            # 补上"这份报告属于哪次输入",否则报告落地后无法自证
            patch = replace(patch, source_fingerprint=fingerprint)

        if patch.depends_on is not None:
            # P3:前置依赖。前置没有"已应用"就一步都不走 —— join_lines 靠它保证
            # 「先并文、后删块」,替换失败时绝不留下被删掉的块。
            dep = next((r for r in records if r.patch_id == patch.depends_on), None)
            if dep is None or dep.status != "applied":
                state = "缺失" if dep is None else dep.status
                records.append(_skipped(
                    patch, "prerequisite_missing",
                    f"前置 Patch {patch.depends_on} 状态为 {state},按依赖不执行",
                ))
                continue

        block = current.block(patch.target_block_id)
        if patch.operation == "set_prefix":
            # 文档级操作:没有块可查(也不该有)。放在块的各项检查之前,
            # 否则会以 target_missing 失败 —— 那是"块不存在",不是"这事不该做"。
            try:
                current, stamped = _apply_prefix(current, patch, applied_at)
            except PatchError as exc:
                records.append(_failed(patch, "operation_mismatch", str(exc)))
                continue
            current = _refresh_meta(current)
            records.append(stamped)
            continue
        if block is None:
            if patch.operation == "delete_block" and _content_gone(current, patch):
                records.append(_skipped(patch, "already_applied",
                                        "目标块已不存在,全文也已无该块内容"))
            else:
                records.append(_failed(patch, "target_missing",
                                       f"目标块不存在: {patch.target_block_id}"))
            continue
        if not block.editable:
            readonly += 1
            records.append(_rejected(
                patch, "readonly_block",
                f"块 {block.id}(kind={block.kind}) 是证据块,不可改写",
            ))
            continue

        if _already_applied(block, patch):
            records.append(_skipped(patch, "already_applied",
                                    "目标块已是 after 状态,未重复修改"))
            continue
        if block.sha256 != patch.before.sha256:
            records.append(_failed(
                patch, "before_mismatch",
                f"before.sha256={patch.before.sha256[:12]} 当前={block.sha256[:12]},未修改",
            ))
            continue
        previous = last_after.get(patch.target_block_id)
        if previous is not None and previous != patch.before.sha256:
            records.append(_failed(
                patch, "before_mismatch",
                "链式校验失败:before 不等于同块上一条已应用的 after",
            ))
            continue

        if patch.confidence == "heuristic" and not allow_heuristic_apply \
                and patch.rule_id not in allowed:
            records.append(_suggested(
                patch, "evidence_insufficient",
                "confidence=heuristic,只记建议,不动正文"
                f"(规则 {patch.rule_id} 未在本次放行名单里)",
            ))
            continue
        if patch.destructive and not patch.evidence.sufficient \
                and not _structural_self_evidence(patch, block):
            records.append(_suggested(
                patch, "evidence_insufficient",
                f"{patch.operation} 缺少可用证据(需结构块/几何证据、"
                f"带坐标的 bbox,或整块被确定性规则完整命中)",
            ))
            continue
        if patch.operation == "replace_text" and patch.after is not None \
                and patch.after.text == block.text \
                and (patch.after.trailer is None
                     or str(block.extra.get("trailer", "")) == patch.after.trailer):
            records.append(_skipped(patch, "no_change", "after 与当前正文相同"))
            continue
        if patch.operation == "merge_blocks":
            records.append(_merge_not_supported(patch))
            continue

        try:
            current, stamped = _apply_one(current, patch, block, applied_at)
        except PatchError as exc:
            records.append(_failed(patch, "operation_mismatch", str(exc)))
            continue
        current = _refresh_meta(current)
        if stamped.after is not None:
            last_after[patch.target_block_id] = stamped.after.sha256
        records.append(stamped)

    md_after = render_ir(current)
    t1 = int(clock()) if finished_at is None else finished_at
    report = CleanReport(
        engine=engine,
        run=RunInfo(
            started_at=t0, finished_at=t1,
            backend=ir.meta.backend or "unknown",
            source_md_sha256=text_sha256(md_before),
            ir_schema=ir.meta.schema,
            options=normalize_options(options),
            rules_enabled=tuple(rules_enabled),
            rules_skipped=tuple(dict(item) for item in rules_skipped),
        ),
        patches=tuple(records),
        stats=build_stats(tuple(records), chars_before=len(md_before),
                          chars_after=len(md_after), blocks_before=blocks_before,
                          blocks_after=len(current.editable_blocks)),
        verification=build_verification(current, md_after=md_after, readonly=readonly,
                                   source_ok=source_ok),
    )
    return ApplyResult(ir=current, report=report, md_before=md_before,
                       md_after=md_after,
                       diff=render_unified_diff(md_before, md_after))


# ---------------------------------------------------------------- 撤销

def _split_block_id(block_id: str) -> tuple[str, int | None]:
    """``<part_id>#m12`` → ``("part_id", 12)``;认不出来就退回 (整本, None)。"""
    head, sep, tail = block_id.rpartition("#m")
    if not sep or not tail.isdigit():
        return ("book", None)
    return (head or "book", int(tail))


def _restored(block: Block, before: BlockState) -> Block:
    """按 ``before`` 快照还原正文、层级与块间空行(位置沿用当前 span 起点)。

    只还原正文是不够的:块间空行与层级同样属于"原样",漏掉它们会让撤销后的
    ``book.md`` 与原文差几个字节 —— 而"差几个字节"正是最难发现的那类回归。
    """
    start = block.md_span[0] if block.md_span else 0
    return replace(
        block,
        text=before.text,
        level=before.level,
        md_span=(start, start + len(before.text)),
        extra={**block.extra, "trailer": before.trailer},
    )


def _reinsert(ir: BookIR, patch: Patch) -> BookIR:
    """把被删的块按报告里的位置与块间空行放回去。"""
    before = patch.before
    part_id, index = _split_block_id(patch.target_block_id)
    # 报告是外部文件:页来源对不上时按"没有页归属"处理,而不是让 Provenance 抛错
    page = before.page_idx
    source = before.page_idx_source
    if page is None or source not in PAGE_SOURCES or source == "none":
        page, source = None, "none"
    prov = Provenance(
        backend=ir.meta.backend or "unknown",
        part_id=part_id,
        block_index=index if index is not None else 0,
        page_idx=page,
        page_idx_source=source,
    )
    block = Block(
        id=patch.target_block_id,
        kind=before.kind if before.kind in KINDS else "text",   # type: ignore[arg-type]
        text=before.text,
        order=before.order if before.order is not None else (index or 0),
        prov=prov,
        kind_confidence=before.kind_confidence
        if before.kind_confidence in ("explicit", "derived", "heuristic") else "derived",
        editable=True,
        md_span=before.md_span,
        level=before.level,
        extra={"trailer": before.trailer},
    )
    return ir.insert_block(block)


def revert_patches(ir: BookIR, report: CleanReport, *,
                   finished_at: int | None = None, clock=time.time) -> ApplyResult:
    """按报告把 ``applied`` 的 Patch 逐条撤回到 ``before``。

    撤销顺序是应用顺序的**逆序**:后改的必须先撤,否则同块上多条 Patch 的
    ``after`` 校验会互相错位。每条仍要过 ``after.sha256`` 校验 —— 报告之后有人
    手改过正文,就必须拒绝,而不是把手工修改一起抹掉。
    """
    md_before = render_ir(ir)
    blocks_before = len(ir.editable_blocks)
    current = ir
    records: list[Patch] = []

    for patch in reversed(report.applied):
        block = current.block(patch.target_block_id)
        if patch.operation == "set_prefix":
            cur = current.meta.prefix
            if cur == patch.before.text:
                records.append(_skipped(patch, "already_reverted", "文首空白已等于 before"))
                continue
            if patch.after is None or cur != patch.after.text:
                records.append(_failed(
                    patch, "after_mismatch",
                    f"文首空白当前 {cur!r} 既不是 after "
                    f"{(patch.after.text if patch.after else '')!r},拒绝恢复",
                ))
                continue
            current = _refresh_meta(replace(
                current, meta=replace(current.meta, prefix=patch.before.text)))
            records.append(patch.with_outcome(
                "applied",
                validation=Validation(True, True, True, None, ("已恢复 before 文首空白",)),
            ))
            continue
        if patch.operation == "delete_block":
            if block is None:
                current = _refresh_meta(_reinsert(current, patch))
                records.append(patch.with_outcome(
                    "applied",
                    validation=Validation(True, True, True, None,
                                          ("已按报告恢复被删块",)),
                ))
            elif block.sha256 == patch.before.sha256:
                records.append(_skipped(patch, "already_reverted", "块已存在且等于 before"))
            else:
                records.append(_failed(patch, "ambiguous_target",
                                       "同 id 的块已存在但正文不等于 before,拒绝覆盖"))
            continue

        if block is None:
            records.append(_failed(patch, "target_missing",
                                   f"目标块不存在: {patch.target_block_id}"))
            continue

        if patch.operation == "set_level":
            # 正文没变,只能靠层级判断"撤没撤过"与"有没有被别人改过"
            if block.level == patch.before.level:
                records.append(_skipped(patch, "already_reverted", "层级已等于 before"))
                continue
            recorded = patch.after.level if patch.after is not None else None
            if block.level != recorded:
                records.append(_failed(
                    patch, "after_mismatch",
                    f"当前层级 H{block.level} 不等于记录 after H{recorded},拒绝恢复",
                ))
                continue
            current = _refresh_meta(
                current.replace_block(block.id, replace(block, level=patch.before.level)))
            records.append(patch.with_outcome(
                "applied",
                validation=Validation(True, True, True, None, ("已恢复 before 层级",)),
            ))
            continue

        if block.sha256 == patch.before.sha256:
            # 正文已等于 before;但带 trailer 变更的 Patch 可能只撤了正文没撤空行
            cur_trailer = str(block.extra.get("trailer", ""))
            if patch.after is not None and patch.after.trailer is not None \
                    and cur_trailer != patch.before.trailer:
                if cur_trailer != patch.after.trailer:
                    records.append(_failed(
                        patch, "after_mismatch",
                        f"块间空行 {cur_trailer!r} 既不是 after {patch.after.trailer!r}"
                        f"也不是 before {patch.before.trailer!r},拒绝恢复",
                    ))
                    continue
                current = _refresh_meta(current.replace_block(
                    block.id, _restored(block, patch.before)))
                records.append(patch.with_outcome(
                    "applied",
                    validation=Validation(True, True, True, None,
                                          ("已恢复 before 块间空行",)),
                ))
                continue
            records.append(_skipped(patch, "already_reverted", "正文已等于 before"))
            continue
        if patch.after is None or block.sha256 != patch.after.sha256:
            records.append(_failed(
                patch, "after_mismatch",
                f"after.sha256={(patch.after.sha256[:12] if patch.after else '')} "
                f"当前={block.sha256[:12]},拒绝恢复(报告之后有人改过正文)",
            ))
            continue
        cur_trailer = str(block.extra.get("trailer", ""))
        if patch.after is not None and patch.after.trailer is not None \
                and cur_trailer not in (patch.after.trailer, patch.before.trailer):
            records.append(_failed(
                patch, "after_mismatch",
                f"块间空行 {cur_trailer!r} 与记录不符,拒绝恢复(报告之后有人改过空行)",
            ))
            continue

        current = _refresh_meta(current.replace_block(
            block.id, _restored(block, patch.before)))
        records.append(patch.with_outcome(
            "applied",
            validation=Validation(True, True, True, None, ("已恢复 before 正文",)),
        ))

    md_after = render_ir(current)
    t1 = int(clock()) if finished_at is None else finished_at
    out = CleanReport(
        engine=report.engine,
        run=RunInfo(
            started_at=report.run.started_at, finished_at=t1,
            backend=report.run.backend,
            source_md_sha256=text_sha256(md_before),
            ir_schema=report.run.ir_schema,
            options=report.run.options,
            rules_enabled=report.run.rules_enabled,
            rules_skipped=report.run.rules_skipped,
        ),
        patches=tuple(records),
        stats=build_stats(tuple(records), chars_before=len(md_before),
                          chars_after=len(md_after), blocks_before=blocks_before,
                          blocks_after=len(current.editable_blocks)),
        verification=build_verification(current, md_after=md_after, readonly=0,
                                   source_ok=True),
    )
    return ApplyResult(ir=current, report=out, md_before=md_before, md_after=md_after,
                       diff=render_unified_diff(md_before, md_after))
