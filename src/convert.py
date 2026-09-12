"""PDF → 统一 Markdown 自动转换入口(idea.md §6 / Phase 6)。

流程:
  1. PDFDetector 检测类型(text / scanned / hybrid)
  2. 自动选择 backend:
       text    → PyMuPDF4LLM(本地)
       scanned → 配置的 OCR backend(云端)
       hybrid  → 页级路由:文字页本地逐页提取,扫描页按**连续区段**渲染为纯图 PDF
                 送 OCR,统一按页单元(PageResult)排序合并
  3. 输出统一 work/book.md + images/
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf
import yaml

from backends import get_backend
from backends.base import Backend, ConversionResult, normalize_image_refs
from detector.pdf_detector import PDFDetector, PDFType
from markdown.bold import annotate_bold
from page_result import (
    PageResult,
    contiguous_runs,
    merge_page_results,
    plan_ocr_runs,
    resolve_ocr_run_limit,
)
from paths import config_file
import events
IMAGES_DIR = "images"


def load_config(config_path: str | Path) -> dict:
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def convert_auto(
    pdf_path: str | Path,
    work_dir: str | Path,
    config: dict | None = None,
    config_path: str | Path | None = None,
) -> tuple[ConversionResult, object]:
    """自动检测并转换,返回 (ConversionResult, DetectionResult)。"""
    if config is None:
        config = load_config(config_path or config_file())
        # 清理开关落到后端配置(如 bold 关闭 → pymupdf.bold_fonts 为空)
        resolve_options(config).apply(config)

    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    detector = PDFDetector()
    events.stage("detect", "start")
    detection = detector.detect(pdf_path)
    print(f"[detect] {detection.summary()}")
    events.emit(
        "detect",
        type=detection.pdf_type.value,
        pages=detection.total_pages,
        text_pages=detection.text_pages,
        text_ratio=round(detection.text_ratio, 4),
        suspicious_pages=detection.suspicious_pages,
    )
    events.stage("detect", "done", type=detection.pdf_type.value,
                 pages=detection.total_pages)

    ocr_name = str(config.get("ocr_backend", "mineru")).lower()
    ocr_cfg = config.get(ocr_name, {})

    if detection.pdf_type == PDFType.TEXT:
        events.emit("plan", backend="pymupdf", pages=detection.total_pages,
                    ocr_pages=0, ocr_runs=0, shards=0)
        events.stage("extract", "start", backend="pymupdf")
        result = get_backend("pymupdf", config.get("pymupdf", {})).convert(pdf_path, work_dir)
    elif detection.pdf_type == PDFType.SCANNED:
        events.emit("plan", backend=ocr_name, pages=detection.total_pages,
                    ocr_pages=detection.total_pages, ocr_runs=1,
                    shards=_planned_shards(detection.total_pages, ocr_cfg))
        events.stage("extract", "start", backend=ocr_name)
        result = get_backend(ocr_name, ocr_cfg).convert(pdf_path, work_dir)
    else:
        hybrid_cfg = config.get("hybrid") or {}
        limit = resolve_ocr_run_limit(hybrid_cfg.get("max_ocr_runs"))
        scanned = [i for i in range(detection.total_pages)
                   if i not in set(detection.text_page_idxs)]
        runs = plan_ocr_runs(scanned, limit)
        events.emit("plan", backend=f"hybrid({ocr_name})", pages=detection.total_pages,
                    ocr_pages=len(scanned), ocr_runs=len(runs),
                    shards=sum(_planned_shards(len(r), ocr_cfg) for r in runs))
        events.stage("extract", "start", backend=f"hybrid({ocr_name})")
        result = _convert_hybrid(
            pdf_path, work_dir, ocr_name, ocr_cfg, detection,
            pymupdf_cfg=config.get("pymupdf", {}),
            max_ocr_runs=hybrid_cfg.get("max_ocr_runs"),
        )
    events.stage("extract", "done", backend=result.backend)

    return result, detection


def _planned_shards(pages: int, ocr_cfg: dict) -> int:
    """按后端单任务页数上限估算云端任务数(与实际提交口径一致,见 dryrun)。"""
    from dryrun import estimate_shards, max_pages_per_task

    return estimate_shards(pages, max_pages_per_task({"mineru": ocr_cfg}, "mineru"))


def _convert_hybrid(
    pdf_path: Path,
    work_dir: Path,
    ocr_name: str,
    ocr_cfg: dict,
    detection,
    pymupdf_cfg: dict | None = None,
    max_ocr_runs: int | str | None = None,
) -> ConversionResult:
    """hybrid 流程:文字页本地逐页提取,扫描页按连续区段送 OCR,按页单元合并。

    页序由 PageResult 承载(每页/每区段带自己的 1-indexed 页码),
    因此 text→scan→text 交错时不会再把整块扫描内容提到最前面。
    """
    # 页级路由与 PDFDetector 判定一致(有效字符 + 乱码率)
    text_idxs = list(detection.text_page_idxs)
    scanned_idxs = [i for i in range(detection.total_pages) if i not in set(text_idxs)]
    pymupdf_cfg = pymupdf_cfg or {}

    images_abs = work_dir / IMAGES_DIR
    images_abs.mkdir(parents=True, exist_ok=True)

    # 1. 文字页 → PyMuPDF4LLM 逐页提取(pages 为 0-indexed)
    results: list[PageResult] = []
    if text_idxs:
        results += get_backend("pymupdf", pymupdf_cfg).page_results(
            pdf_path, images_abs, text_idxs
        )

    # 2. 扫描页 → 连续区段,每段一次云端 OCR(区段位置即原文位置)
    runs = contiguous_runs(scanned_idxs)
    limit = resolve_ocr_run_limit(max_ocr_runs)
    planned = plan_ocr_runs(scanned_idxs, limit)
    if len(planned) < len(runs):
        print(
            f"[hybrid] ⚠ 扫描页分为 {len(runs)} 个区段,超过上限 {limit},"
            f"已合并为 {len(planned)} 段以控制云端任务数;"
            "被合并区段内的页序可能与原文不一致(内容不丢)"
        )
    runs = planned
    if runs:
        ocr = get_backend(ocr_name, ocr_cfg)
        for i, run in enumerate(runs, start=1):
            events.progress("extract", i, len(runs), detail="hybrid_ocr",
                            page=run[0] + 1)
            results.append(_ocr_run(ocr, pdf_path, work_dir, images_abs, run))

    print(
        f"[hybrid] 文字页 {len(text_idxs)} 页, 扫描页 {len(scanned_idxs)} 页"
        f" → {len(results)} 个页单元(其中 OCR {len(runs)} 段)"
    )

    # 3. 按页序合并(页码注释单调递增,便于定位与回归断言)
    merged = merge_page_results(results)

    # 4. 强调字体标注(与纯文字版同一条链路:hybrid 曾经漏掉这一步)
    bold_fonts = list(pymupdf_cfg.get("bold_fonts") or [])
    if bold_fonts:
        merged = annotate_bold(merged, pdf_path, extra_bold_fonts=bold_fonts)

    merged = normalize_image_refs(merged, images_abs)
    book_md = work_dir / "book.md"
    book_md.write_text(merged, encoding="utf-8")

    return ConversionResult(
        book_md=book_md,
        images_dir=images_abs,
        backend=f"hybrid({ocr_name})",
        stats={
            "chars": len(merged),
            "images": len(list(images_abs.glob("*"))) if images_abs.is_dir() else 0,
            "pages": detection.total_pages,
            "text_pages": len(text_idxs),
            "scanned_pages": len(scanned_idxs),
            "ocr_runs": len(runs),
        },
    )


def _ocr_run(
    ocr: Backend,
    pdf_path: Path,
    work_dir: Path,
    images_abs: Path,
    run: list[int],
) -> PageResult:
    """把一个连续扫描区段(0-indexed)渲染成纯图 PDF 送 OCR,返回页单元。"""
    page_no = run[0] + 1                      # 1-indexed 首页页码
    tmp_pdf = work_dir / f"_hybrid_scan_p{page_no}.pdf"
    run_work = work_dir / f"_scan_p{page_no}"

    _render_pages_to_pdf(pdf_path, run, tmp_pdf)
    try:
        scan = ocr.convert(tmp_pdf, run_work)
        md = scan.book_md.read_text(encoding="utf-8")
        md = _absorb_images(run_work / IMAGES_DIR, images_abs, md,
                            prefix=f"scan_p{page_no}_")
    except Exception:
        # OCR 失败:保留 run_work —— 里面的 .ocr_task.json 记录着已提交的云端
        # 任务,重跑时可直接续跑(不重新上传、不重复扣额度)
        tmp_pdf.unlink(missing_ok=True)
        raise
    tmp_pdf.unlink(missing_ok=True)
    shutil.rmtree(run_work, ignore_errors=True)

    return PageResult(
        pages=tuple(p + 1 for p in run),   # 0-indexed → 1-indexed 原始页码
        source="ocr",
        markdown=md,
        backend=scan.backend,
    )


def _absorb_images(src_dir: Path, dst_dir: Path, md: str, *, prefix: str) -> str:
    """把 OCR 子目录的图片并入统一 images/;重名时加前缀并同步改引用。"""
    if not src_dir.is_dir():
        return md
    for img in sorted(src_dir.iterdir()):
        if not img.is_file():
            continue
        target = dst_dir / img.name
        if target.exists():
            new_name = f"{prefix}{img.name}"
            img.replace(dst_dir / new_name)
            md = _rename_image_refs(md, img.name, new_name)
        else:
            img.replace(target)
    return md


def _rename_image_refs(md: str, old_name: str, new_name: str) -> str:
    """改掉 Markdown 里指向 old_name 的图片引用(只认 `](images/...)` 形式)。"""
    for form in (f"]({IMAGES_DIR}/{old_name})", f"]({IMAGES_DIR}/{old_name} "):
        md = md.replace(form, form.replace(old_name, new_name))
    return md


def _render_pages_to_pdf(src_pdf: Path, page_idxs: list[int], out_pdf: Path) -> None:
    """把指定页渲染为纯图 PDF(无文字层),用于 OCR。"""
    src = pymupdf.open(src_pdf)
    out = pymupdf.open()
    try:
        for idx in page_idxs:
            page = src[idx]
            pix = page.get_pixmap(dpi=150)
            new_page = out.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=pix.tobytes("png"))
        out.save(out_pdf)
    finally:
        src.close()
        out.close()
