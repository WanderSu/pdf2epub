"""清理器 v2 的 Patch 契约(阶段 B §E.7 的 schema v1 实现)。

一份 ``clean-report.json`` 的全部价值在于**可验证**:每条 Patch 都带着
「改之前是什么、改之后是什么、凭什么改、改成功没有」。所以这里的每个字段
都不是装饰:

``before`` / ``after``
    精确原文 + UTF-8 字节的 SHA-256。应用前比 ``before``,恢复前比 ``after`` ——
    两个方向都要能判定「这份报告还成立吗」,不能只信任报告里的说法。
``before.md_span`` / ``order`` / ``trailer`` / ``kind``
    ``delete_block`` 之后要能把块**原样插回去**,而插入位置与块间空行只能从报告
    里读 —— 回读时重新去 md 里找位置就是模糊匹配,禁止。
``evidence``
    这条判断凭的是什么。``sufficient`` 决定「能不能动正文」:纯文本规则不足以
    支撑删除/合并,必须叠加结构证据或几何证据。
``status``
    ``candidate``(规则给的候选,尚未处理)/ ``applied`` / ``suggested``(建议但
    证据不足,不动正文)/ ``skipped`` / ``rejected`` / ``failed``。报告必须能把
    这六种分开,否则「改了没改」只能靠 diff 猜。
``source_fingerprint``
    这份报告属于哪本书的哪次输入。换了一次 OCR 产物,旧报告必须失效。

相对 §E.7 的**加法**(都是可选字段,旧读端可以忽略,不影响 schema_version):

- ``before`` 多了 ``level/order/trailer/kind/kind_confidence`` —— 为了 revert 自足;
- ``status`` 多了 ``candidate`` 与 ``unknown``(用户要求区分「候选」;未知枚举按
  §E.7 读成 ``unknown`` 且永不执行);
- ``skip_reason`` 多了 ``target_missing/already_applied/already_reverted/
  source_mismatch/operation_mismatch/no_change`` —— 每一种都对应一条真实失败路径,
  合并成一个 ``ambiguous_target`` 会让排障重新变成猜。

P3 加法(仍是可选字段,旧读端忽略):

- ``after.trailer`` —— ``replace_text`` 允许连同**块间空行**一起替换(join_lines
  把 B 并进 A 时,A 与下文之间的空行必须变成 B 原来的 trailer,否则拼完多出一截
  空行)。``None`` = 不改 trailer;``set_level``/``noop`` 不允许带。
- ``depends_on`` —— 本 Patch 只在前置 Patch **已 applied** 时才执行,否则记
  ``skipped/prerequisite_missing``。join_lines 用它把「先并文、后删块」表达成
  显式依赖:前置替换没成功,绝不动删除。只能指向**同一次执行序列里更早**的
  patch_id(向前的依赖按缺失处理)。

P5 加法:

- ``set_prefix`` —— 文档级操作,改 ``meta.prefix``(文首空白)。它不属于任何块,
  所以 ``target_block_id`` 固定为 :data:`PREFIX_TARGET`;``before``/``after``
  只用到 ``text``/``sha256``(没有 span、层级、块间空行可谈)。引入它的理由:
  ``blank_lines`` 要压缩的空白里,最前面那一段不在任何块的 ``trailer`` 里 ——
  没有这个操作就只剩"报 note 但改不了"这一条路,字节一致性永远差几个字符。

P6 加法(旧读端可忽略):

- ``evidence.scope`` —— ``partial``(缺省,片段命中)/ ``whole``(命中覆盖整块)。
  整块命中是"这一块都该删"的必要条件,引擎还要核对 ``confidence`` 与 ``matched``
  是否真的等于块正文;不做这一步,「删页码行/空标题」这类确定性判断就只能永远停在
  建议,而放开 ``sufficient`` 又会把「文本规则猜测删正文」一起放进来。
- ``RULE_IDS`` 末尾多 ``trailing_spaces`` —— 行尾空白在 legacy 里也是**无条件**步骤,
  不是九个开关之一;同样要走 Patch 账,就得有 rule_id。
- ``replace_text`` 允许带 ``after.level`` —— ``headings`` 要在同一条 Patch 里同时改
  "`#` 的个数"与"标题文本形态"(拆两条会让后一条的 ``before`` 被前一条改掉,
  链式校验当场失效)。撤销时 ``_restored`` 照样按 ``before`` 一次性还原文与层级。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable

from ir.model import Block, BookIR, IRError, text_sha256
from ir.render import render_ir

#: ``clean-report.json`` 的信封版本。不兼容变更才 +1(§E.7)。
SCHEMA_VERSION = 1

#: 九个清理项的键名(**顺序即报告里 ``by_rule`` 的顺序**,与 cleaner.CLEAN_KEYS 一致),
#: 末尾 ``blank_lines`` 是 P4 加法:它不是九个开关之一(legacy 里是**无条件**步骤),
#: 但要走同一套 Patch 账,就得有 rule_id。加在末尾不改动既有顺序。
RULE_IDS: tuple[str, ...] = (
    "page_numbers", "running_heads", "join_lines", "ocr_spaces", "cjk_spaces",
    "dup_headings", "headings", "bold", "images",
    # --- P4 加法:空行压缩(legacy 里是无条件步骤,不是九个开关之一)---
    "blank_lines",
    # --- P6 加法:行尾空白(同上,legacy 里也是无条件步骤)---
    "trailing_spaces",
)

OPERATIONS: tuple[str, ...] = (
    "replace_text", "delete_block", "merge_blocks", "set_level", "noop",
    # --- P5 加法:文档级(文首空白) ---
    "set_prefix",
)

#: ``set_prefix`` 的固定目标 id。文首空白不属于任何块(它是 ``meta.prefix``),
#: 但 Patch 的 ``target_block_id`` 不允许为空 —— 用一个不可能与真实块 id 相同的
#: 名字,既满足"目标必须是可指名的东西",也让读端一眼看出这不是块级操作。
#: 真实块 id 形如 ``<part>#m12`` / ``<part>#e12``,撞不上 ``@``。
PREFIX_TARGET = "book@prefix"

#: 状态顺序即报告里 ``by_status`` 的顺序
STATUSES: tuple[str, ...] = (
    "candidate", "applied", "suggested", "skipped", "rejected", "failed", "unknown",
)

SKIP_REASONS: tuple[str, ...] = (
    "no_evidence", "evidence_insufficient", "ambiguous_target", "before_mismatch",
    "after_mismatch", "readonly_block", "rule_disabled", "not_in_markdown",
    "legacy_fallback_forbidden",
    # --- v1 加法(见模块文档) ---
    "target_missing", "already_applied", "already_reverted", "source_mismatch",
    "operation_mismatch", "no_change",
    # --- P3 加法 ---
    "prerequisite_missing",
)

CONFIDENCES: tuple[str, ...] = ("explicit", "derived", "heuristic")
RISKS: tuple[str, ...] = ("low", "medium", "high")
EVIDENCE_SOURCES: tuple[str, ...] = (
    "evidence_block", "md_block", "geometry", "text_rule", "unknown",
)

RULE_ID_SET = frozenset(RULE_IDS)
OPERATION_SET = frozenset(OPERATIONS)
STATUS_SET = frozenset(STATUSES)
CONFIDENCE_SET = frozenset(CONFIDENCES)
RISK_SET = frozenset(RISKS)
EVIDENCE_SOURCE_SET = frozenset(EVIDENCE_SOURCES)
#: ``evidence.scope``:命中范围(见 ``Evidence`` 的说明)
EVIDENCE_SCOPES: tuple[str, ...] = ("partial", "whole")
EVIDENCE_SCOPE_SET = frozenset(EVIDENCE_SCOPES)

_PATCH_ID_RE = re.compile(r"^p\d{4,}$")


class PatchError(IRError):
    """Patch 构造/校验失败(不产出半份报告)。"""


class ReportVersionError(PatchError):
    """报告版本不支持 —— 读端必须拒绝恢复,不得猜字段(§E.7)。"""


def _need(value: str, allowed: Iterable[str], what: str) -> str:
    if value not in set(allowed):
        raise PatchError(f"未知的 {what}: {value!r}(允许: {', '.join(sorted(allowed))})")
    return value


def _need_or_unknown(value: str, allowed: Iterable[str], what: str) -> str:
    """同 ``_need``,但放行 ``unknown``。

    ``unknown`` 是**读入侧**的合法取值(报告里出现未来版本的枚举),构造侧允许它
    存在、但引擎见到就不执行 —— 这样「不认识」和「写错了」仍然分得开:
    写错时 ``_norm_enum`` 也只会得到 unknown,而我们的代码不会主动传 unknown。
    """
    return _need(value, set(allowed) | {"unknown"}, what)


def _norm_enum(value: object, allowed: Iterable[str], what: str) -> str:
    """读入侧归一:未知取值读成 ``unknown``,而不是抛异常。

    §E.7 的原话是「读端把未知枚举当 unknown 处理,且永不据此执行」。构造侧
    (``Patch(...)``)仍然严格抛错 —— 那是我们自己的代码写错了,越早炸越好。
    """
    text = str(value or "")
    return text if text in set(allowed) else "unknown"


def _norm_optional(value: object, allowed: Iterable[str]) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return text if text in set(allowed) else "unknown"


@dataclass(frozen=True)
class Evidence:
    """一条判断的证据。``matched`` 是真正命中的原始字符串(不是改写后的)。

    ``scope``(P6 加法)说明 ``matched`` 覆盖了多大范围:

    - ``partial``(缺省):在某一行/某个片段里命中 —— 支撑"改这一行",不支撑删整块;
    - ``whole``:命中**覆盖整块**(``matched`` 就是整块正文)。只有这一种能支撑
      "这一整块都该删"的判断,而且还要看 ``Patch.confidence``(推导,不是猜测)。
      比对放在 ``clean.engine._structural_self_evidence``:引擎手里有块,由它核对
      ``matched == 块正文``,而不是让规则自己声明"我命中整块"就算数。
    """

    kind: str = "unknown"
    page_idx: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    bbox_space: str = ""
    text_level: int | None = None
    matched: str = ""
    source: str = "md_block"
    scope: str = "partial"

    def __post_init__(self) -> None:
        _need(self.source, EVIDENCE_SOURCE_SET, "evidence.source")
        if self.scope not in EVIDENCE_SCOPE_SET:
            raise PatchError(f"未知的 evidence.scope: {self.scope!r}")
        if self.bbox is not None and not self.bbox_space:
            # 有 bbox 没有坐标系 → 与 IR 同规则:视为没有 bbox,不假装能比较
            object.__setattr__(self, "bbox", None)

    @property
    def sufficient(self) -> bool:
        """够不够支撑「动正文」(删除/合并)。

        判据:必须有命中文本,且证据要么来自结构块/几何,要么带坐标系明确的 bbox。
        纯文本正则(``text_rule``)单独不算 —— 历史上 13 处 join_lines 拼接里
        12 处是这么误伤的(§F#3)。

        ``scope="whole"`` **不在这里**放行:整块命中只是"这一块该删"的必要条件,
        还要引擎同时核对 ``confidence``(推导 vs 猜测)与 ``matched`` 是否真的等于
        块正文(见 ``clean.engine._structural_self_evidence``)。
        """
        if not self.matched.strip():
            return False
        if self.source in ("evidence_block", "geometry"):
            return True
        return self.bbox is not None and bool(self.bbox_space)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "page_idx": self.page_idx,
            "bbox": list(self.bbox) if self.bbox else None,
            "bbox_space": self.bbox_space,
            "text_level": self.text_level,
            "matched": self.matched,
            "source": self.source,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Evidence":
        data = data or {}
        raw_bbox = data.get("bbox")
        bbox = None
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                bbox = tuple(float(v) for v in raw_bbox)  # type: ignore[assignment]
            except (TypeError, ValueError):
                bbox = None
        return cls(
            kind=str(data.get("kind") or "unknown"),
            page_idx=data.get("page_idx"),
            bbox=bbox,
            bbox_space=str(data.get("bbox_space") or ""),
            text_level=data.get("text_level"),
            matched=str(data.get("matched") or ""),
            source=_norm_enum(data.get("source"), EVIDENCE_SOURCE_SET, "evidence.source")
            if data.get("source") not in (None, "") else "unknown",
            # 旧报告没有 scope:按 partial 读(那时没有整块命中的概念)
            scope=str(data.get("scope"))
            if data.get("scope") in EVIDENCE_SCOPE_SET else "partial",
        )


@dataclass(frozen=True)
class BlockState:
    """块在某一时刻的**完整**快照:正文、哈希、位置、块间空行、类型。

    ``sha256`` 可省略(构造时算),但一旦给了就必须与 ``text`` 相符 ——
    允许「哈希与原文不符」的报告存在,等于允许伪造 before。
    """

    text: str
    sha256: str = ""
    md_span: tuple[int, int] | None = None
    level: int | None = None
    order: int | None = None
    trailer: str = ""
    kind: str = "text"
    kind_confidence: str = "explicit"
    page_idx: int | None = None
    page_idx_source: str = "none"

    def __post_init__(self) -> None:
        if not self.sha256:
            object.__setattr__(self, "sha256", text_sha256(self.text))
        elif self.sha256 != text_sha256(self.text):
            raise PatchError(
                f"before.sha256 与 before.text 不符(文本 sha={text_sha256(self.text)[:12]},"
                f"记录={self.sha256[:12]})"
            )
        if self.md_span is not None:
            start, end = self.md_span
            if start < 0 or end < start or end - start != len(self.text):
                raise PatchError(f"before.md_span 与 before.text 长度不符: {self.md_span}")

    @classmethod
    def of(cls, block: Block) -> "BlockState":
        return cls(
            text=block.text,
            sha256=block.sha256,
            md_span=block.md_span,
            level=block.level,
            order=block.order,
            trailer=str(block.extra.get("trailer", "")),
            kind=block.kind,
            kind_confidence=block.kind_confidence,
            page_idx=block.prov.page_idx,
            page_idx_source=block.prov.page_idx_source,
        )

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "sha256": self.sha256,
            "md_span": list(self.md_span) if self.md_span else None,
            "level": self.level,
            "order": self.order,
            "trailer": self.trailer,
            "kind": self.kind,
            "kind_confidence": self.kind_confidence,
            "page_idx": self.page_idx,
            "page_idx_source": self.page_idx_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BlockState":
        span = data.get("md_span")
        return cls(
            text=str(data.get("text", "")),
            sha256=str(data.get("sha256") or ""),
            md_span=(int(span[0]), int(span[1])) if span else None,
            level=data.get("level"),
            order=data.get("order"),
            trailer=str(data.get("trailer") or ""),
            kind=str(data.get("kind") or "text"),
            kind_confidence=str(data.get("kind_confidence") or "explicit"),
            page_idx=data.get("page_idx"),
            page_idx_source=str(data.get("page_idx_source") or "none"),
        )


@dataclass(frozen=True)
class AfterState:
    """改完之后的正文与层级。``delete_block`` 时整条 after 为 ``None``。

    ``trailer`` 是 P3 加法:``replace_text`` 同时改块间空行时记录**改后的**
    空行(撤销时 ``before.trailer`` 自足)。``None`` = 不改。
    """

    text: str
    sha256: str = ""
    level: int | None = None
    trailer: str | None = None

    def __post_init__(self) -> None:
        if not self.sha256:
            object.__setattr__(self, "sha256", text_sha256(self.text))
        elif self.sha256 != text_sha256(self.text):
            raise PatchError(
                f"after.sha256 与 after.text 不符(文本 sha={text_sha256(self.text)[:12]},"
                f"记录={self.sha256[:12]})"
            )
        if self.level is not None and not (1 <= self.level <= 6):
            raise PatchError(f"after.level 越界: {self.level}")

    def to_dict(self) -> dict:
        return {"text": self.text, "sha256": self.sha256, "level": self.level,
                "trailer": self.trailer}

    @classmethod
    def from_dict(cls, data: dict | None) -> "AfterState | None":
        if not data:
            return None
        raw_trailer = data.get("trailer")
        return cls(
            text=str(data.get("text", "")),
            sha256=str(data.get("sha256") or ""),
            level=data.get("level"),
            trailer=None if raw_trailer is None else str(raw_trailer),
        )


@dataclass(frozen=True)
class SourceFingerprint:
    """这份报告属于哪本书的哪次输入(§E.7)。"""

    book_md_sha256: str
    ir_schema: int = 1
    backend: str = "unknown"
    variant: str = "original"

    @classmethod
    def of(cls, ir: BookIR) -> "SourceFingerprint":
        return cls(
            book_md_sha256=text_sha256(render_ir(ir)),
            ir_schema=ir.meta.schema,
            backend=ir.meta.backend or "unknown",
            variant=ir.meta.variant or "unknown",
        )

    def matches(self, other: "SourceFingerprint") -> bool:
        return (
            self.book_md_sha256 == other.book_md_sha256
            and self.ir_schema == other.ir_schema
            and self.backend == other.backend
            and self.variant == other.variant
        )

    def to_dict(self) -> dict:
        return {
            "book_md_sha256": self.book_md_sha256,
            "ir_schema": self.ir_schema,
            "backend": self.backend,
            "variant": self.variant,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "SourceFingerprint":
        data = data or {}
        return cls(
            book_md_sha256=str(data.get("book_md_sha256") or ""),
            ir_schema=int(data.get("ir_schema") or 0),
            backend=str(data.get("backend") or "unknown"),
            variant=str(data.get("variant") or "unknown"),
        )


@dataclass(frozen=True)
class Validation:
    """这条 Patch 的校验结果。``notes`` 是给人看的,不进任何判定。"""

    before_ok: bool = False
    after_ok: bool = False
    kind_confidence_ok: bool = True
    applied_at: int | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "before_ok": self.before_ok,
            "after_ok": self.after_ok,
            "kind_confidence_ok": self.kind_confidence_ok,
            "applied_at": self.applied_at,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Validation":
        data = data or {}
        return cls(
            before_ok=bool(data.get("before_ok")),
            after_ok=bool(data.get("after_ok")),
            kind_confidence_ok=bool(data.get("kind_confidence_ok", True)),
            applied_at=data.get("applied_at"),
            notes=tuple(str(n) for n in (data.get("notes") or ())),
        )


@dataclass(frozen=True)
class Patch:
    """一条可应用、可撤销、可审计的修改意图书。"""

    patch_id: str
    rule_id: str
    target_block_id: str
    operation: str
    before: BlockState
    after: AfterState | None
    evidence: Evidence = field(default_factory=Evidence)
    confidence: str = "explicit"
    risk: str = "low"
    status: str = "candidate"
    skip_reason: str | None = None
    source_fingerprint: SourceFingerprint | None = None
    depends_on: str | None = None
    validation: Validation = field(default_factory=Validation)

    def __post_init__(self) -> None:
        if not _PATCH_ID_RE.match(self.patch_id or ""):
            raise PatchError(f"patch_id 必须是 p+至少 4 位数字: {self.patch_id!r}")
        _need_or_unknown(self.rule_id, RULE_ID_SET, "rule_id")
        _need_or_unknown(self.operation, OPERATION_SET, "operation")
        _need_or_unknown(self.confidence, CONFIDENCE_SET, "confidence")
        _need_or_unknown(self.risk, RISK_SET, "risk")
        _need(self.status, STATUS_SET, "status")
        if self.skip_reason is not None:
            _need_or_unknown(self.skip_reason, set(SKIP_REASONS), "skip_reason")
        if not self.target_block_id:
            raise PatchError("target_block_id 不能为空")
        if self.operation == "set_prefix":
            if self.target_block_id != PREFIX_TARGET:
                raise PatchError(
                    f"set_prefix 的目标必须是 {PREFIX_TARGET!r}: {self.target_block_id!r}")
        elif self.target_block_id == PREFIX_TARGET:
            raise PatchError(
                f"{self.target_block_id!r} 是文档级目标,只能用 operation='set_prefix'")
        if self.depends_on is not None:
            if not _PATCH_ID_RE.match(self.depends_on):
                raise PatchError(f"depends_on 必须是 patch_id 形状: {self.depends_on!r}")
            if self.depends_on == self.patch_id:
                raise PatchError("depends_on 不能指向自己")
        if self.operation == "delete_block":
            if self.after is not None:
                raise PatchError("delete_block 的 after 必须是 null(没有改后正文)")
        elif self.operation == "unknown":
            pass            # 未来版本的 operation:结构不校验,引擎也不会执行
        elif self.after is None:
            raise PatchError(f"{self.operation} 必须给出 after")
        if self.operation in ("set_level", "noop") and self.after is not None:
            if self.after.text != self.before.text:
                raise PatchError(f"{self.operation} 不得改变正文")
            if self.after.trailer is not None:
                raise PatchError(f"{self.operation} 不得携带 after.trailer(改空行走 replace_text)")
        if self.operation == "set_prefix" and self.after is not None:
            if self.after.text == self.before.text:
                raise PatchError("set_prefix 的 after 与 before 完全相同")
            if self.after.trailer is not None:
                raise PatchError("set_prefix 不涉及块间空行,不得携带 after.trailer")
            if self.after.level is not None:
                raise PatchError("set_prefix 不涉及层级,不得携带 after.level")

    # --- 便捷视图 ---
    @property
    def destructive(self) -> bool:
        return self.operation in ("delete_block", "merge_blocks")

    @property
    def decided(self) -> bool:
        """规则已经给出结论(不需要引擎再动手)。"""
        return self.status in ("suggested", "skipped", "rejected", "failed", "unknown")

    def with_outcome(self, status: str, *, skip_reason: str | None = None,
                     validation: Validation | None = None,
                     before: BlockState | None = None,
                     after: AfterState | None = None) -> "Patch":
        return replace(
            self,
            status=status,
            skip_reason=skip_reason,
            validation=validation if validation is not None else self.validation,
            before=before if before is not None else self.before,
            after=after if after is not None else self.after,
        )

    # --- 构造入口 ---
    @classmethod
    def propose_prefix(cls, *, patch_id: str, rule_id: str, before_text: str,
                       after_text: str, evidence: Evidence | None = None,
                       confidence: str = "derived", risk: str = "low",
                       source_fingerprint: SourceFingerprint | None = None) -> "Patch":
        """文首空白(``meta.prefix``)的候选 Patch。

        与 ``propose`` 的区别只有"before 从哪来":文档级操作没有块可读,``before``
        直接就是当前的 ``prefix`` 原文。其余契约完全一致 —— 应用前比 ``before``
        哈希,撤销前比 ``after`` 哈希。
        """
        return cls(
            patch_id=patch_id, rule_id=rule_id, target_block_id=PREFIX_TARGET,
            operation="set_prefix",
            before=BlockState(text=before_text),
            after=AfterState(text=after_text),
            evidence=evidence or Evidence(), confidence=confidence, risk=risk,
            status="candidate", skip_reason=None,
            source_fingerprint=source_fingerprint,
        )

    @classmethod
    def propose(cls, *, patch_id: str, rule_id: str, block: Block, operation: str,
                after_text: str | None = None, level: int | None = None,
                evidence: Evidence | None = None, confidence: str = "explicit",
                risk: str = "low",
                source_fingerprint: SourceFingerprint | None = None) -> "Patch":
        """从**当前块**造一条候选 Patch(``status="candidate"``)。

        规则层只负责说「我想把这块改成什么」,``before`` 的完整快照由这里取,
        免得每条规则各写一份快照逻辑、各漏一个字段(漏 ``trailer`` 就没法 revert)。
        """
        before = BlockState.of(block)
        after: AfterState | None
        if operation == "delete_block":
            after = None
        else:
            after = AfterState(
                text=block.text if after_text is None else after_text,
                level=block.level if level is None else level,
            )
        return cls(
            patch_id=patch_id, rule_id=rule_id, target_block_id=block.id,
            operation=operation, before=before, after=after,
            evidence=evidence or Evidence(), confidence=confidence, risk=risk,
            status="candidate", skip_reason=None,
            source_fingerprint=source_fingerprint,
        )

    # --- 序列化(字段顺序固定,差分友好) ---
    def to_dict(self) -> dict:
        return {
            "patch_id": self.patch_id,
            "rule_id": self.rule_id,
            "target_block_id": self.target_block_id,
            "operation": self.operation,
            "before": self.before.to_dict(),
            "after": self.after.to_dict() if self.after is not None else None,
            "evidence": self.evidence.to_dict(),
            "confidence": self.confidence,
            "risk": self.risk,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "source_fingerprint": (self.source_fingerprint.to_dict()
                                   if self.source_fingerprint is not None else None),
            "depends_on": self.depends_on,
            "validation": self.validation.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Patch":
        """读入侧:未知枚举归一为 ``unknown``,其余字段严格。

        未知枚举不会导致读失败(否则一份含新规则的旧报告就整份作废),
        但引擎见到 ``unknown`` 一律不执行 —— 见 ``Patch.decided``。
        """
        before = BlockState.from_dict(data.get("before") or {})
        raw_status = data.get("status")
        status = _norm_enum(raw_status, STATUS_SET, "status") if raw_status else "unknown"
        return cls(
            patch_id=str(data.get("patch_id") or ""),
            rule_id=_norm_enum(data.get("rule_id"), RULE_ID_SET, "rule_id"),
            target_block_id=str(data.get("target_block_id") or ""),
            operation=_norm_enum(data.get("operation"), OPERATION_SET, "operation"),
            before=before,
            after=AfterState.from_dict(data.get("after")),
            evidence=Evidence.from_dict(data.get("evidence")),
            confidence=_norm_enum(data.get("confidence"), CONFIDENCE_SET, "confidence"),
            risk=_norm_enum(data.get("risk"), RISK_SET, "risk"),
            status=status,
            skip_reason=_norm_optional(data.get("skip_reason"), set(SKIP_REASONS)),
            source_fingerprint=(SourceFingerprint.from_dict(data["source_fingerprint"])
                                if data.get("source_fingerprint") else None),
            depends_on=(str(data["depends_on"]) if data.get("depends_on") else None),
            validation=Validation.from_dict(data.get("validation")),
        )


def sort_patches(patches: Iterable[Patch]) -> tuple[Patch, ...]:
    """按 ``patch_id`` 升序(报告展示用;执行顺序以调用方给的列表为准)。"""
    return tuple(sorted(patches, key=lambda p: p.patch_id))
