"""转换预检(--dry-run,计划 1.6)。

只做「检测 + 规划」:**不创建 work//output 目录、不调用任何后端、不产出文件**,
让用户在真正转换(尤其是消耗云端 OCR 额度)之前看清将要发生什么:

  - 每个文件的类型(文字版 / 扫描版 / 混合)与页数
  - 计划使用的后端(本地 PyMuPDF4LLM 还是云端 OCR,哪个云服务)
  - 云端分片数(按 max_pages_per_task)
  - 需 OCR 的合计页数是否超过当日额度(超出建议分批)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from detector.pdf_detector import PDFDetector, PDFType

#: 云端 OCR 每日额度(页)。MinerU Cloud 免费额度为 1000 页/日;
#: 超出后提交会失败,预检阶段就提醒用户分批。
DAILY_OCR_PAGE_QUOTA = 1000

#: 单个云端任务默认页数上限(config 未配置时的兜底)
DEFAULT_MAX_PAGES_PER_TASK = 200


def estimate_shards(pages: int, per_task: int) -> int:
    """按「每任务页数上限」估算云端分片数(0 页 → 0 段)。"""
    if pages <= 0:
        return 0
    per_task = max(1, int(per_task or DEFAULT_MAX_PAGES_PER_TASK))
    return math.ceil(pages / per_task)


def max_pages_per_task(config: dict, backend: str) -> int:
    """该后端单任务页数上限(config 里可覆盖,默认 200)。"""
    value = (config.get(backend) or {}).get("max_pages_per_task")
    try:
        return int(value) if value else DEFAULT_MAX_PAGES_PER_TASK
    except (TypeError, ValueError):
        return DEFAULT_MAX_PAGES_PER_TASK


@dataclass
class PlanItem:
    """单个文件的转换计划。"""

    source: Path
    kind: str = ""            # pdf-text / pdf-scanned / pdf-hybrid / markdown
    backend: str = ""         # pymupdf / mineru / paddleocr / hybrid(mineru) / markdown
    pages: int = 0
    ocr_pages: int = 0        # 需要送云端 OCR 的页数
    shards: int = 0           # 云端分片数
    text_pages: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def needs_ocr(self) -> bool:
        return self.ocr_pages > 0


def plan_source(
    source: Path,
    config: dict,
    backend_override: str | None = None,
    detector: PDFDetector | None = None,
) -> PlanItem:
    """为单个文件生成计划(只读检测,不写盘)。"""
    source = Path(source)
    item = PlanItem(source=source)

    if source.suffix.lower() != ".pdf":
        item.kind = "markdown"
        item.backend = "markdown"
        item.notes.append("已有 Markdown:清理后直接生成 EPUB,不消耗 OCR 额度")
        return item

    ocr_name = str(config.get("ocr_backend", "mineru")).lower()
    detection = (detector or PDFDetector()).detect(source)
    item.pages = detection.total_pages
    item.text_pages = detection.text_pages

    if backend_override and backend_override != "auto":
        item.backend = backend_override
        if backend_override == "pymupdf":
            item.kind = "pdf-text"
            item.notes.append("手动指定本地提取:不做 OCR")
        else:
            item.kind = "pdf-scanned"
            item.ocr_pages = detection.total_pages
            item.notes.append("手动指定云端 OCR:全篇按 OCR 处理(不看文字层检测结果)")
    elif detection.pdf_type == PDFType.TEXT:
        item.kind = "pdf-text"
        item.backend = "pymupdf"
    elif detection.pdf_type == PDFType.SCANNED:
        item.kind = "pdf-scanned"
        item.backend = ocr_name
        item.ocr_pages = detection.total_pages
    else:
        item.kind = "pdf-hybrid"
        item.backend = f"hybrid({ocr_name})"
        item.ocr_pages = detection.total_pages - detection.text_pages

    if detection.suspicious_pages > 0 and detection.pdf_type != PDFType.SCANNED:
        item.notes.append(
            f"{detection.suspicious_pages}/{detection.total_pages} 页疑似文字层损坏(乱码),"
            f"如需 OCR 请用 --backend {ocr_name}"
        )
    if item.needs_ocr:
        per_task = max_pages_per_task(config, ocr_name)
        item.shards = estimate_shards(item.ocr_pages, per_task)
        item.notes.append(f"按 {per_task} 页/任务分片")
    return item


def plan(
    paths: list[Path],
    config: dict,
    backend_override: str | None = None,
    detector: PDFDetector | None = None,
    quota: int = DAILY_OCR_PAGE_QUOTA,
) -> list[PlanItem]:
    """为一批输入生成计划(仅支持已存在的文件/目录展开,目录内文件同样只检测)。"""
    from batch import iter_sources   # 延迟导入:避免与 batch 循环依赖

    items: list[PlanItem] = []
    for src in iter_sources([Path(p) for p in paths]):
        try:
            items.append(plan_source(src, config, backend_override, detector))
        except Exception as e:  # noqa: BLE001 - 预检不应因单个坏文件中断
            failed = PlanItem(source=src, kind="error")
            failed.notes.append(f"检测失败: {e}")
            items.append(failed)
    return items


def render(
    items: list[PlanItem],
    config: dict | None = None,
    backend_override: str | None = None,
    quota: int = DAILY_OCR_PAGE_QUOTA,
) -> str:
    """人类可读的预检报告(dry-run 输出)。"""
    config = config or {}
    lines = ["[预检] 只做检测与规划,不产出任何文件"]

    for item in items:
        lines.append("")
        lines.append(f"  {item.source.name}")
        lines.append(f"    类型: {item.kind}")
        if item.backend:
            lines.append(f"    后端: {item.backend}")
        if item.pages:
            detail = f"    页数: {item.pages}"
            if item.needs_ocr:
                detail += f"(需 OCR {item.ocr_pages} 页,文字层可用 {item.text_pages} 页)"
            lines.append(detail)
        if item.shards:
            lines.append(f"    分片: {item.shards} 段")
        for note in item.notes:
            lines.append(f"    · {note}")

    total_ocr = sum(i.ocr_pages for i in items)
    total_shards = sum(i.shards for i in items)
    ocr_files = sum(1 for i in items if i.needs_ocr)
    lines.append("")
    lines.append(f"[预检] 合计 {len(items)} 个文件,{ocr_files} 个需云端 OCR,"
                 f"共 {total_ocr} 页 / 当日额度 {quota} 页")
    if total_ocr > quota:
        lines.append(f"[预检] ⚠ 预计分片 {total_shards} 段,超出当日额度,建议分批转换")
    elif total_ocr:
        lines.append(f"[预检] 预计分片 {total_shards} 段,未超出当日额度")
    return "\n".join(lines)
