"""页归属审计(P4):把「哪些块有页号、哪些没有、为什么」变成可查的账。

页归属在本次重构里是**页覆盖校验**与**按页定位问题**的地基,所以这里只做一件事:
照实统计 + 报问题,**不补页号**。理由写在 ``ir.align`` 的模块文档里 —— 补出来的
页号会让页覆盖校验失去意义(它算出来的"覆盖"其实是自己的猜测)。

三种缺失,性质完全不同,必须分开报:

``unavailable``(可接受)
    这份 IR **根本没有证据层**(后端没给 ``content_list``)。此时缺页是输入形态
    决定的,不是对齐失败 —— 报 ``info``,不计入失败。
``exempt``(可接受)
    页码注释块自带 ``extra['page']``、分隔线不承载内容,它们不需要从证据层
    继承页号。报都不报,只计入 ``exempt``。
``missing``(可接受的缺口,但要可定位)
    有证据层却没配上。这是对齐的已知局限(真实书实测覆盖率 95%+),不是数据矛盾。
    报 ``warning`` 并把块 id / 类型 / 原文片段写出来,供人工复核。

硬错误(``error``)只留给**数据自相矛盾**,因为那说明某处已经不可信:

- ``page_out_of_range`` 页号超出 ``total_pages``;
- ``page_regression`` 页号随阅读顺序倒退(对齐游标单调,倒退意味着两边顺序已乱);
- ``source_missing`` 有页号却说不出来源(模型层已挡,这里是读外部数据时的兜底)。

``producer_unknown``(有证据层但 ``producer.name`` 是 ``unknown``)是 **warning**
而不是 error:``build_ir`` 在未显式给出 producer 时按 v1 契约记 ``unknown``(不从
``backend`` 推断),这是**允许的调用方式**。把它判成硬错误会让「按规矩不猜产出方」
这件事本身变成失败 —— 它只是让这份账归不到产出方名下。

``coverage`` 只在 ``required > 0`` 时有值;没有可归属的块时是 ``None`` 而不是 0.0,
免得"无从归属"被读成"归属率 0%"。

**零归属页**(P5):``gaps`` 把每个"一个块都没归属上"的页按证据层判成因 ——
``blank``(证据层里什么都没留下)/ ``no_text``(有区域、没文字,可能是图版页)/
``unattributed``(有正文却没归属,真缺口)/ ``unavailable``(没有证据层)。
``no_text`` 刻意**不**判成空白页:那需要渲染 PDF 对照(本阶段不渲染),而
"判不了"必须如实写成"判不了",不能拿一个好看的标签盖过去。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ir.model import UNKNOWN, BookIR, IRError

#: 严重级别。``error`` 会传给严格模式抛异常,``warning``/``info`` 只记录。
SEVERITIES: tuple[str, ...] = ("error", "warning", "info")

#: 问题代码(固定顺序 = 报告里的分组顺序)
ISSUE_CODES: tuple[str, ...] = (
    "page_out_of_range", "page_regression", "source_missing", "producer_unknown",
    "missing_attribution", "uncovered_page",
)

#: 不需要从证据层继承页号的块:页码注释自带页号,分隔线不承载内容
_EXEMPT_ROLES = frozenset({"page_marker", "horizontal_rule"})

#: 证据层里只是**页面装饰**的类型:它们证明"这一页存在",不证明"这一页有内容"
_DECORATION_KINDS = frozenset({"page_number", "header", "footer"})

#: 零归属页的成因(顺序即报告里的顺序,读端可据此差分)
#:
#: ``blank``         证据层里这一页没有任何条目(或只有页码/页眉页脚)
#: ``no_text``       有版面区域,但没有可识别文字(插图/图版,或**跨页段落的续页**
#:                   —— 后端把两页并成一个块时,续页只剩一个空区域)
#: ``unattributed``  有**非空内容**证据却没有块归属 —— 这才是真正的对齐缺口
#: ``unavailable``   整份 IR 没有证据层,无从判定
GAP_STATES: tuple[str, ...] = ("blank", "no_text", "unattributed", "unavailable")


class PageAuditError(IRError):
    """页归属审计失败(严格模式下的硬错误)。"""


def _exempt(block) -> bool:
    """这个块是否豁免页归属(自带页号 / 不承载内容)。"""
    role = str(block.extra.get("role") or "")
    if role in _EXEMPT_ROLES:
        return True
    return False


@dataclass(frozen=True)
class PageIssue:
    """一条可定位的页归属问题。

    ``block_id`` / ``order`` / ``kind`` / ``snippet`` 是定位用的四件套:只报
    「有 N 个块缺页」等于让人重新跑一遍才知道是哪 N 个。
    """

    severity: str
    code: str
    block_id: str = ""
    order: int | None = None
    kind: str = ""
    page_idx: int | None = None
    page_idx_source: str = "none"
    detail: str = ""
    snippet: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise PageAuditError(f"未知的 severity: {self.severity!r}")
        if self.code not in ISSUE_CODES:
            raise PageAuditError(f"未知的问题代码: {self.code!r}")

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "block_id": self.block_id,
            "order": self.order,
            "kind": self.kind,
            "page_idx": self.page_idx,
            "page_idx_source": self.page_idx_source,
            "detail": self.detail,
            "snippet": self.snippet,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PageIssue":
        return cls(
            severity=str(data.get("severity") or "info"),
            code=str(data.get("code") or "uncovered_page"),
            block_id=str(data.get("block_id") or ""),
            order=data.get("order"),
            kind=str(data.get("kind") or ""),
            page_idx=data.get("page_idx"),
            page_idx_source=str(data.get("page_idx_source") or "none"),
            detail=str(data.get("detail") or ""),
            snippet=str(data.get("snippet") or ""),
        )


def _snippet(block) -> str:
    text = block.text.strip().replace("\n", " ")
    return text[:40]


@dataclass(frozen=True)
class PageAttribution:
    """一份 IR 的页归属账。字段顺序固定,读出来可以做差分。"""

    producer: str = UNKNOWN
    producer_known: bool = False
    #: ``unavailable``(没有证据层) / ``partial`` / ``complete``
    evidence_state: str = "unavailable"
    total_editable: int = 0
    required: int = 0
    exempt: int = 0
    attributed: int = 0
    missing: int = 0
    coverage: float | None = None
    by_kind: dict = field(default_factory=dict)
    by_source: dict = field(default_factory=dict)
    issues: tuple[PageIssue, ...] = ()
    uncovered_pages: tuple[int, ...] = ()
    #: 零归属页的逐页成因(P5 加法;``uncovered_pages`` 是它的页号投影)
    gaps: tuple[PageGap, ...] = ()

    # --- 缺口视图 ---
    def gap_pages(self, state: str) -> tuple[int, ...]:
        """某一类零归属页的页号(1-based)。"""
        return tuple(g.page for g in self.gaps if g.state == state)

    # --- 视图 ---
    @property
    def errors(self) -> tuple[PageIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[PageIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def ok(self) -> bool:
        """没有硬错误。缺页不算错 —— 它是可接受的缺口,由调用方决定阈值。"""
        return not self.errors

    def raise_for_errors(self) -> "PageAttribution":
        """严格模式:有硬错误就抛(供 P5 的闸门调用)。"""
        if self.errors:
            first = self.errors[0]
            raise PageAuditError(
                f"页归属审计失败:{len(self.errors)} 个硬错误,首条 {first.code}"
                f"({first.block_id or '-'}: {first.detail})"
            )
        return self

    # --- 序列化 ---
    def to_dict(self) -> dict:
        return {
            "producer": self.producer,
            "producer_known": self.producer_known,
            "evidence_state": self.evidence_state,
            "total_editable": self.total_editable,
            "required": self.required,
            "exempt": self.exempt,
            "attributed": self.attributed,
            "missing": self.missing,
            "coverage": self.coverage,
            "by_kind": self.by_kind,
            "by_source": self.by_source,
            "issues": [i.to_dict() for i in self.issues],
            "uncovered_pages": list(self.uncovered_pages),
            "gaps": [g.to_dict() for g in self.gaps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PageAttribution":
        return cls(
            producer=str(data.get("producer") or UNKNOWN),
            producer_known=bool(data.get("producer_known")),
            evidence_state=str(data.get("evidence_state") or "unavailable"),
            total_editable=int(data.get("total_editable") or 0),
            required=int(data.get("required") or 0),
            exempt=int(data.get("exempt") or 0),
            attributed=int(data.get("attributed") or 0),
            missing=int(data.get("missing") or 0),
            coverage=data.get("coverage"),
            by_kind=dict(data.get("by_kind") or {}),
            by_source=dict(data.get("by_source") or {}),
            issues=tuple(PageIssue.from_dict(i) for i in (data.get("issues") or ())),
            uncovered_pages=tuple(int(p) for p in (data.get("uncovered_pages") or ())),
            gaps=tuple(PageGap.from_dict(g) for g in (data.get("gaps") or ())),
        )


# ---------------------------------------------------------------- 页覆盖

def covered_pages(ir: BookIR) -> tuple[int, ...]:
    """有块归属的**页号**,与 ``ir.pages`` 同一口径(1-based)。

    ``page_idx`` 是 0-based(后端给的就是这样),``ir.pages`` 是 1-based
    (``range(1, total_pages + 1)``)。直接拿两个集合做差会把整套页号错位一格 ——
    表现为「明明每页都有内容,却报全部页未覆盖」。所以换算只在这里做一次。
    """
    return tuple(sorted({b.page_idx + 1 for b in ir.editable_blocks
                         if b.page_idx is not None}))


def uncovered_pages(ir: BookIR) -> tuple[int, ...]:
    """``ir.pages`` 里**没有任何块归属**的页号(1-based)。

    空白页是合法结果,所以这不是错误。想知道"零归属"到底是哪一类,用
    :func:`page_gaps` —— 它把每一页按证据层分成可判定 / 不可判定。
    """
    if not ir.pages:
        return ()
    covered = set(covered_pages(ir))
    return tuple(p for p in ir.pages if p not in covered)


# ---------------------------------------------------------------- 零归属成因

@dataclass(frozen=True)
class PageGap:
    """一个零归属页的成因判定(只依据证据层,不做任何推断)。

    ``evidence`` / ``content`` / ``regions`` 三个计数是判定依据本身:报告里给出
    "为什么这么判",比给一个标签更有用 —— 复核时能直接看到该页在 content_list
    里有几个条目、其中几个带正文。
    """

    page: int                      # 1-based,与 ir.pages 同口径
    state: str                     # 见 GAP_STATES
    evidence: int = 0              # 该页证据块数(含页码/页眉页脚)
    content: int = 0               # 其中带正文/表格体/图片的条数
    regions: int = 0               # 其中"有版面区域但没有文字"的条数
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in GAP_STATES:
            raise PageAuditError(f"未知的零归属成因: {self.state!r}")

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "state": self.state,
            "evidence": self.evidence,
            "content": self.content,
            "regions": self.regions,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PageGap":
        return cls(
            page=int(data.get("page") or 0),
            state=str(data.get("state") or "unavailable"),
            evidence=int(data.get("evidence") or 0),
            content=int(data.get("content") or 0),
            regions=int(data.get("regions") or 0),
            detail=str(data.get("detail") or ""),
        )


def _evidence_has_content(block) -> bool:
    """这条证据是否**承载内容**(能支撑"这一页不是空白页"的判断)。

    页码/页眉/页脚只证明"这一页被扫过",不证明"这一页有正文"。
    """
    if block.kind in _DECORATION_KINDS:
        return False
    if block.text.strip():
        return True
    if block.kind == "image" and str(block.extra.get("img_path") or "").strip():
        return True
    if block.kind == "table" and str(block.extra.get("table_body") or "").strip():
        return True
    return False


def _evidence_is_region(block) -> bool:
    """有版面区域却没有可识别文字(插图区/图版/扫描空白)。"""
    if block.kind in _DECORATION_KINDS or _evidence_has_content(block):
        return False
    return block.prov.bbox is not None or block.kind in ("text", "heading", "table")


def page_gaps(ir: BookIR) -> tuple[PageGap, ...]:
    """把每个零归属页按**证据层**判成因;判不了的如实标成判不了。

    判据只有一条:**这一页在 content_list 里留下了什么**。

    - 什么内容条目都没有 → ``blank``(证据层里这页是空的);
    - 只有"有区域、无文字"的条目 → ``no_text``。**不判成 blank**:那等于替
      一张图版页签"空白页"的字,而这件事只能靠渲染证实(本阶段没有渲染产物);
    - 有非空正文却没有块归属 → ``unattributed``,这是真正的对齐缺口;
    - 整份 IR 没有证据层 → ``unavailable``(输入形态,不是失败)。

    这里**不补页号、不改块**,只给出可与证据对照的判定 —— 复核者拿着
    ``evidence/content/regions`` 三个计数就能自己重算。
    """
    unclaimed = uncovered_pages(ir)
    if not unclaimed:
        return ()
    by_page: dict[int, list] = {}
    for block in ir.evidence_blocks:
        page = block.prov.page_idx
        if page is None:
            continue
        by_page.setdefault(page + 1, []).append(block)

    has_layer = bool(ir.evidence_blocks)
    gaps: list[PageGap] = []
    for page in unclaimed:
        entries = by_page.get(page, [])
        content = [b for b in entries if _evidence_has_content(b)]
        regions = [b for b in entries if _evidence_is_region(b)]
        if not has_layer:
            state = "unavailable"
            detail = "没有证据层,这一页有没有内容无从判定(不猜)"
        elif content:
            state = "unattributed"
            detail = (f"证据层有 {len(content)} 条内容条目,但没有块归属到这一页"
                      f"(对齐缺口,不补页号)")
        elif regions:
            state = "no_text"
            detail = (f"{len(regions)} 个版面区域没有可识别文字。两种成因都不补页号:"
                      f"图版/插图页(该页本来没有文字),或**跨页段落的续页**"
                      f"(后端把两页并成一个块,续页只剩空区域);要渲染 PDF 才能分开")
        else:
            state = "blank"
            detail = "证据层里这一页没有任何内容条目(只有页码/页眉页脚或什么都没有)"
        gaps.append(PageGap(page=page, state=state, evidence=len(entries),
                            content=len(content), regions=len(regions),
                            detail=detail))
    return tuple(gaps)


def gaps_by_state(ir: BookIR, state: str) -> tuple[int, ...]:
    """某一类零归属页的页号(1-based)。``state`` 必须是 :data:`GAP_STATES` 之一。"""
    if state not in GAP_STATES:
        raise PageAuditError(f"未知的零归属成因: {state!r}")
    return tuple(g.page for g in page_gaps(ir) if g.state == state)


# ---------------------------------------------------------------- 审计入口

def audit_page_attribution(ir: BookIR, *, snippet_chars: int = 40,
                           max_issues: int = 200) -> PageAttribution:
    """统计页归属并列出可定位的问题。**不改任何块**,也不补页号。

    ``max_issues`` 只截断 ``issues`` 列表(便于日志),计数不受影响 ——
    「截断了 200 条」与「只有 200 条」在报告里必须能区分,所以 ``missing``
    这类计数始终是全量。
    """
    blocks = sorted(ir.editable_blocks, key=lambda b: b.order)
    producer = ir.meta.producer
    total = ir.meta.total_pages
    has_evidence = bool(ir.evidence_blocks)
    if not has_evidence:
        state = "unavailable"
    elif not any(b.page_idx is None for b in blocks if not _exempt(b)):
        state = "complete"
    else:
        state = "partial"

    required_blocks = [b for b in blocks if not _exempt(b)]
    attributed = [b for b in required_blocks if b.page_idx is not None]
    missing = [b for b in required_blocks if b.page_idx is None]

    by_kind: dict[str, dict[str, int]] = {}
    for b in required_blocks:
        row = by_kind.setdefault(b.kind, {"required": 0, "attributed": 0, "missing": 0})
        row["required"] += 1
        row["attributed" if b.page_idx is not None else "missing"] += 1
    by_source: dict[str, int] = {}
    for b in blocks:
        key = b.prov.page_idx_source or "none"
        by_source[key] = by_source.get(key, 0) + 1

    issues: list[PageIssue] = []

    if has_evidence and not producer.known:
        issues.append(PageIssue(
            "warning", "producer_unknown",
            detail=(f"证据层存在但 producer.name=unknown,这份账归不到产出方名下"
                    f"(variant={producer.variant};不猜)"),
        ))

    for b in required_blocks:
        page = b.page_idx
        source = b.prov.page_idx_source or "none"
        if page is None:
            if not has_evidence:
                severity = "info"
                detail = "没有证据层,页归属不可得(输入形态如此,非对齐失败)"
            else:
                severity = "warning"
                detail = "有证据层但未对齐到任何证据块,页归属留空(不补页号)"
            issues.append(PageIssue(severity, "missing_attribution", b.id, b.order,
                                    b.kind, None, source, detail, _snippet(b)[:snippet_chars]))
            continue
        if source == "none":
            issues.append(PageIssue("error", "source_missing", b.id, b.order, b.kind,
                                    page, source, "有页号但 page_idx_source=none",
                                    _snippet(b)[:snippet_chars]))
        if total > 0 and not (0 <= page < total):
            issues.append(PageIssue("error", "page_out_of_range", b.id, b.order, b.kind,
                                    page, source,
                                    f"页号 {page + 1} 超出 1..{total}",
                                    _snippet(b)[:snippet_chars]))

    # 页序倒退:对齐游标单调,倒退意味着 md 段序与证据块序已经不一致
    last = -1
    for b in attributed:
        page = b.page_idx if b.page_idx is not None else -1
        if page < last:
            issues.append(PageIssue("error", "page_regression", b.id, b.order, b.kind,
                                    b.page_idx, b.prov.page_idx_source,
                                    f"页号 {page + 1} 早于前面已归属的 {last + 1} 页",
                                    _snippet(b)[:snippet_chars]))
        else:
            last = page

    gaps = page_gaps(ir)
    if gaps:
        counts = {state: sum(1 for g in gaps if g.state == state)
                  for state in GAP_STATES}
        parts = [f"{state} {counts[state]}" for state in GAP_STATES if counts[state]]
        # 三种成因的处置完全不同(blank 正常 / no_text 待渲染 / unattributed 是缺口),
        # 合成一句"可能是空白页"等于把该查的那一页也放过
        issues.append(PageIssue(
            "warning" if counts["unattributed"] else "info", "uncovered_page",
            detail=(f"{len(gaps)} 页没有任何块归属({', '.join(parts)});"
                    f"逐页成因见 report.gaps"),
        ))

    return PageAttribution(
        producer=producer.name,
        producer_known=producer.known,
        evidence_state=state,
        total_editable=len(blocks),
        required=len(required_blocks),
        exempt=len(blocks) - len(required_blocks),
        attributed=len(attributed),
        missing=len(missing),
        coverage=(round(len(attributed) / len(required_blocks), 6)
                  if required_blocks else None),
        by_kind={k: by_kind[k] for k in sorted(by_kind)},
        by_source={k: by_source[k] for k in sorted(by_source)},
        issues=tuple(issues[:max_issues]),
        uncovered_pages=tuple(g.page for g in gaps),
        gaps=gaps,
    )


def page_coverage(ir: BookIR) -> float | None:
    """归属覆盖率(0~1);没有需要归属的块时返回 ``None``。"""
    return audit_page_attribution(ir).coverage


def format_attribution(report: PageAttribution, *,
                       issues: int = 10) -> str:
    """给人看的一段摘要(日志 / 阶段报告用)。"""
    cov = "n/a" if report.coverage is None else f"{report.coverage:.1%}"
    lines = [
        f"producer={report.producer} known={report.producer_known} "
        f"evidence={report.evidence_state}",
        f"可编辑块 {report.total_editable}:需归属 {report.required},"
        f"豁免 {report.exempt},已归属 {report.attributed},缺失 {report.missing}",
        f"覆盖率 {cov};按来源 {report.by_source}",
    ]
    for kind, row in report.by_kind.items():
        if row["missing"]:
            lines.append(f"  缺页集中在 {kind}: {row['missing']}/{row['required']}")
    for state in GAP_STATES:
        pages = report.gap_pages(state)
        if pages:
            lines.append(f"  零归属页[{state}] {len(pages)} 页: {list(pages[:12])}"
                         f"{' …' if len(pages) > 12 else ''}")
    for issue in report.issues[:issues]:
        loc = issue.block_id or "-"
        lines.append(f"  [{issue.severity}] {issue.code} {loc}: {issue.detail}")
    if len(report.issues) > issues:
        lines.append(f"  …另有 {len(report.issues) - issues} 条")
    return "\n".join(lines)


def audit_many(irs: Iterable[BookIR]) -> tuple[PageAttribution, ...]:
    """批量审计(多段/多书);保持入参顺序。"""
    return tuple(audit_page_attribution(ir) for ir in irs)
