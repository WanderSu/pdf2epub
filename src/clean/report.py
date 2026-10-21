"""``clean-report.json`` 的信封与统计(阶段 B §E.7)。

报告是清理器 v2 唯一的对外契约,``clean.diff`` 只给人看(见 ``clean.diff`` 模块)。
这里的三个结构分别回答三个问题:

``run``           这次跑的是什么引擎、什么选项、哪份输入
``stats``         改了多少(按规则、按状态、按字符)
``verification``  改完之后文档还自洽吗(往返、页覆盖、只读违规、输入指纹)

确定性:字段顺序固定、``ensure_ascii=False``、无浮点、末尾一个换行。同输入同选项
两次运行,除 ``run.started_at/finished_at`` 外**逐字节相同** —— 否则差分测试没法用。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from clean.options import CLEAN_KEYS, CleanOptions
from clean.patch import (RULE_IDS, SCHEMA_VERSION, STATUSES, Patch,
                         ReportVersionError)

REPORT_FILE = "clean-report.json"
DIFF_FILE = "clean.diff"

ENGINES = ("ir", "legacy")


def default_options() -> dict:
    """九个清理项的默认开关(与 collector.config / 桌面端保持一致)。

    不在这里重写一份默认值:默认值的唯一来源是 ``clean.options.CleanOptions``,
    复制一份迟早会有一边忘了改。
    """
    base = CleanOptions()
    return {key: bool(getattr(base, key)) for key in CLEAN_KEYS}


def normalize_options(options: dict | None) -> dict:
    """只保留九个键,顺序固定;缺的补默认值(旧配置缺键不该让报告变形)。"""
    merged = default_options()
    for key, value in (options or {}).items():
        if key in merged:
            merged[key] = bool(value)
    return merged


@dataclass(frozen=True)
class RunInfo:
    started_at: int = 0
    finished_at: int = 0
    backend: str = "unknown"
    source_md_sha256: str = ""
    ir_schema: int = 1
    options: dict = field(default_factory=dict)
    rules_enabled: tuple[str, ...] = ()
    rules_skipped: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "backend": self.backend,
            "source_md_sha256": self.source_md_sha256,
            "ir_schema": self.ir_schema,
            "options": self.options,
            "rules_enabled": list(self.rules_enabled),
            "rules_skipped": [dict(item) for item in self.rules_skipped],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "RunInfo":
        data = data or {}
        return cls(
            started_at=int(data.get("started_at") or 0),
            finished_at=int(data.get("finished_at") or 0),
            backend=str(data.get("backend") or "unknown"),
            source_md_sha256=str(data.get("source_md_sha256") or ""),
            ir_schema=int(data.get("ir_schema") or 0),
            options=normalize_options(data.get("options")),
            rules_enabled=tuple(str(x) for x in (data.get("rules_enabled") or ())),
            rules_skipped=tuple(dict(item) for item in (data.get("rules_skipped") or ())),
        )


@dataclass(frozen=True)
class Stats:
    by_rule: dict = field(default_factory=dict)
    by_status: dict = field(default_factory=dict)
    chars_before: int = 0
    chars_after: int = 0
    blocks_before: int = 0
    blocks_after: int = 0

    def to_dict(self) -> dict:
        return {
            "by_rule": self.by_rule,
            "by_status": self.by_status,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "blocks_before": self.blocks_before,
            "blocks_after": self.blocks_after,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Stats":
        data = data or {}
        return cls(
            by_rule=dict(data.get("by_rule") or {}),
            by_status=dict(data.get("by_status") or {}),
            chars_before=int(data.get("chars_before") or 0),
            chars_after=int(data.get("chars_after") or 0),
            blocks_before=int(data.get("blocks_before") or 0),
            blocks_after=int(data.get("blocks_after") or 0),
        )


@dataclass(frozen=True)
class Verification:
    """改完之后的自检结论。全是布尔/计数,不含判断依据 —— 依据在各 Patch 里。"""

    roundtrip_ok: bool = False
    pages: dict = field(default_factory=dict)
    page_attribution_missing: int = 0
    readonly_violations: int = 0
    #: v1 加法:输入指纹是否与报告一致。不一致时所有 Patch 都会失败,
    #: 但这件事必须单独可见,不能只体现在 12 条 failed 里。
    source_ok: bool = True

    def to_dict(self) -> dict:
        return {
            "roundtrip_ok": self.roundtrip_ok,
            "pages": self.pages,
            "page_attribution_missing": self.page_attribution_missing,
            "readonly_violations": self.readonly_violations,
            "source_ok": self.source_ok,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Verification":
        data = data or {}
        return cls(
            roundtrip_ok=bool(data.get("roundtrip_ok")),
            pages=dict(data.get("pages") or {}),
            page_attribution_missing=int(data.get("page_attribution_missing") or 0),
            readonly_violations=int(data.get("readonly_violations") or 0),
            source_ok=bool(data.get("source_ok", True)),
        )


def _count_by(values: list[str], order: tuple[str, ...]) -> dict:
    """按**固定顺序**计数(只列出现过的),保证两次运行顺序相同。"""
    seen: dict[str, int] = {}
    for value in values:
        seen[value] = seen.get(value, 0) + 1
    ordered = {key: seen[key] for key in order if key in seen}
    for key in sorted(set(seen) - set(order)):
        ordered[key] = seen[key]
    return ordered


@dataclass(frozen=True)
class CleanReport:
    schema_version: int = SCHEMA_VERSION
    engine: str = "ir"
    run: RunInfo = field(default_factory=RunInfo)
    patches: tuple[Patch, ...] = ()
    stats: Stats = field(default_factory=Stats)
    verification: Verification = field(default_factory=Verification)
    #: P6 加法:**只报结论、不产 Patch 的检查**(图片引用缺失、规则说明、输入归一化)。
    #: 与 Patch 分开是因为它们是"观察"而不是"改动":混进 patches 会让
    #: "改了几处"这个数字失去意义。
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ReportVersionError(
                f"clean-report schema_version={self.schema_version} 不支持"
                f"(本实现只写 {SCHEMA_VERSION})"
            )
        if self.engine not in ENGINES:
            raise ReportVersionError(f"未知的 engine: {self.engine!r}")

    # --- 视图 ---
    def by_status(self, status: str) -> tuple[Patch, ...]:
        return tuple(p for p in self.patches if p.status == status)

    @property
    def applied(self) -> tuple[Patch, ...]:
        return self.by_status("applied")

    @property
    def candidates(self) -> tuple[Patch, ...]:
        return self.by_status("candidate")

    @property
    def failed(self) -> tuple[Patch, ...]:
        return self.by_status("failed")

    def patch(self, patch_id: str) -> Patch | None:
        for item in self.patches:
            if item.patch_id == patch_id:
                return item
        return None

    # --- 序列化 ---
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "run": self.run.to_dict(),
            "patches": [p.to_dict() for p in self.patches],
            "stats": self.stats.to_dict(),
            "verification": self.verification.to_dict(),
            "issues": list(self.issues),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CleanReport":
        if not isinstance(data, dict):
            raise ReportVersionError("clean-report 必须是 JSON 对象")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            # 拒绝 + 由调用方 warn:猜版本会把新字段当旧字段用,比报错危险
            raise ReportVersionError(
                f"clean-report schema_version={version!r} 与读端({SCHEMA_VERSION})不符,"
                f"拒绝恢复;如需读取请用对应版本的解析器"
            )
        return cls(
            schema_version=int(version),
            engine=str(data.get("engine") or "ir"),
            run=RunInfo.from_dict(data.get("run")),
            patches=tuple(Patch.from_dict(row) for row in (data.get("patches") or ())),
            stats=Stats.from_dict(data.get("stats")),
            verification=Verification.from_dict(data.get("verification")),
            issues=tuple(str(x) for x in (data.get("issues") or ())),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def deterministic_view(self) -> dict:
        """抹掉时间戳的视图(差分与确定性断言用)。"""
        data = self.to_dict()
        data["run"]["started_at"] = 0
        data["run"]["finished_at"] = 0
        for patch in data["patches"]:
            patch["validation"]["applied_at"] = None
        return data


def build_stats(patches: tuple[Patch, ...], *, chars_before: int, chars_after: int,
                blocks_before: int, blocks_after: int) -> Stats:
    return Stats(
        by_rule=_count_by([p.rule_id for p in patches], RULE_IDS),
        by_status=_count_by([p.status for p in patches], STATUSES),
        chars_before=chars_before,
        chars_after=chars_after,
        blocks_before=blocks_before,
        blocks_after=blocks_after,
    )


def write_report(report: CleanReport, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json(), encoding="utf-8")
    return out


def read_report(path: str | Path) -> CleanReport:
    """读回报告。版本不符抛 ``ReportVersionError``(不降级、不猜字段)。"""
    return CleanReport.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
