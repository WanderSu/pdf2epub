"""PDF → 统一 Markdown 自动转换入口(idea.md §6 / Phase 6)。

流程:
  1. PDFDetector 检测类型(text / scanned / hybrid)
  2. 自动选择 backend:
       text    → PyMuPDF4LLM(本地)
       scanned → 配置的 OCR backend(云端)
       hybrid  → 页级路由:文字页本地提取,扫描页渲染为纯图 PDF 送 OCR,按页码合并
  3. 输出统一 work/book.md + images/
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pymupdf4llm
import yaml

from backends import get_backend
from backends.base import ConversionResult, normalize_image_refs
from detector.pdf_detector import PDFDetector, PDFType
from paths import config_file

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

    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    detector = PDFDetector()
    detection = detector.detect(pdf_path)
    print(f"[detect] {detection.summary()}")

    ocr_name = str(config.get("ocr_backend", "mineru")).lower()
    ocr_cfg = config.get(ocr_name, {})

    if detection.pdf_type == PDFType.TEXT:
        result = get_backend("pymupdf", config.get("pymupdf", {})).convert(pdf_path, work_dir)
    elif detection.pdf_type == PDFType.SCANNED:
        result = get_backend(ocr_name, ocr_cfg).convert(pdf_path, work_dir)
    else:
        result = _convert_hybrid(pdf_path, work_dir, ocr_name, ocr_cfg, detection)

    return result, detection


def _convert_hybrid(
    pdf_path: Path,
    work_dir: Path,
    ocr_name: str,
    ocr_cfg: dict,
    detection,
) -> ConversionResult:
    """hybrid 流程:文字页本地提取,扫描页渲染为纯图 PDF 送 OCR,按页合并。"""
    threshold = 50  # 与 PDFDetector 默认一致
    text_idxs = [i for i, n in enumerate(detection.page_char_counts) if n >= threshold]
    scanned_idxs = [i for i, n in enumerate(detection.page_char_counts) if n < threshold]
    print(f"[hybrid] 文字页 {len(text_idxs)} 页, 扫描页 {len(scanned_idxs)} 页")

    # 1. 文字页 → PyMuPDF4LLM(页号 1-indexed)
    scan_work: Path | None = None
    if text_idxs:
        text_md = pymupdf4llm.to_markdown(
            str(pdf_path),
            pages=[i + 1 for i in text_idxs],
            write_images=True,
            image_path=str(work_dir / IMAGES_DIR),
        )
    else:
        text_md = ""

    # 2. 扫描页 → 渲染纯图临时 PDF → OCR backend
    scan_md = ""
    if scanned_idxs:
        tmp_pdf = work_dir / "_hybrid_scan_pages.pdf"
        _render_pages_to_pdf(pdf_path, scanned_idxs, tmp_pdf)
        scan_work = work_dir / "_scan"
        scan_result = get_backend(ocr_name, ocr_cfg).convert(tmp_pdf, scan_work)
        scan_md = scan_result.book_md.read_text(encoding="utf-8")
        tmp_pdf.unlink(missing_ok=True)

    # 3. 按页码顺序合并(用页标记分隔,便于定位)
    parts: list[tuple[int, str]] = []
    if text_idxs:
        parts.append((min(text_idxs), text_md))
    if scanned_idxs:
        parts.append((min(scanned_idxs), scan_md))
    parts.sort(key=lambda x: x[0])
    merged = "\n\n".join(f"<!-- page-group {p} -->\n{md.strip()}" for p, md in parts)

    # 4. 把 OCR 子目录图片合并进统一 images/,并修正引用
    images_abs = work_dir / IMAGES_DIR
    images_abs.mkdir(parents=True, exist_ok=True)
    scan_images = scan_work / IMAGES_DIR if scan_work else None
    if scan_images and scan_images.is_dir():
        for img in sorted(scan_images.iterdir()):
            if not img.is_file():
                continue
            target = images_abs / img.name
            if target.exists():
                # 重名冲突:加 scan_ 前缀并替换引用
                new_name = f"scan_{img.name}"
                img.replace(images_abs / new_name)
                merged = merged.replace(f"]({IMAGES_DIR}/{img.name}", f"]({IMAGES_DIR}/{new_name}")
            else:
                img.replace(target)
        import shutil

        shutil.rmtree(scan_work, ignore_errors=True)

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
            "text_pages": len(text_idxs),
            "scanned_pages": len(scanned_idxs),
        },
    )


def _render_pages_to_pdf(src_pdf: Path, page_idxs: list[int], out_pdf: Path) -> None:
    """把指定页渲染为纯图 PDF(无文字层),用于 OCR。"""
    src = pymupdf.open(src_pdf)
    out = pymupdf.open()
    for idx in page_idxs:
        page = src[idx]
        pix = page.get_pixmap(dpi=150)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=pix.tobytes("png"))
    out.save(out_pdf)
