"""批处理核心(idea.md §15 / Phase 7)。

能力:
  - 批量处理(文件或目录,串行执行,优先稳定性)
  - 日志(控制台 + 文件)
  - 失败重试(指数退避)
  - 跳过已完成文件(EPUB 存在且不早于源文件)
  - 断点续跑(重跑时自动跳过已完成)
  - 单个文件失败不中断整体

状态判定:output/<name>.epub 存在且 mtime ≥ 源文件 mtime → 已完成。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from backends import get_backend
from convert import convert_auto, load_config
from epub.pandoc import build_epub
from epub.verify import VerifyResult, verify_epub
from markdown.cleaner import CleanOptions, clean_file, resolve_options
from detector.pdf_detector import PDFType
from paths import config_file

logger = logging.getLogger("pdf2epub.batch")

SUPPORTED_SUFFIXES = {".pdf", ".md"}


class VerifyError(RuntimeError):
    """EPUB 结构校验未通过(--strict)。确定性失败:不重试。"""


def sanitize_name(name: str) -> str:
    """规范化**工作目录**名:空白与非法字符 → 下划线。
    必要原因:PyMuPDF 的 C 库保存图片时会把路径中的空格替换为下划线,
    含空格的工作目录会导致图片写入失败。
    """
    name = re.sub(r"[\s]+", "_", name.strip())
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    return name or "book"


def output_stem(name: str) -> str:
    """输出 EPUB 的**文件名**(保留原文空格)。

    与工作目录不同,EPUB 由 Pandoc 写出、不经过 PyMuPDF,无需把空格换成下划线:
    保留「标题 - 作者」原貌更易读,也让书库的「 - 」分隔解析与 EPUB 元数据一致。
    仅替换 Windows 非法字符并去掉结尾空白/点。
    """
    name = re.sub(r'[\\/:*?"<>|]+', "_", name.strip()).rstrip(". ").strip()
    return name or "book"


def epub_path(source: Path, output_dir: Path) -> Path:
    """输出 EPUB 路径(保留原文空格,与 output_stem 一致)。"""
    return output_dir / f"{output_stem(source.stem)}.epub"


def existing_epub(source: Path, output_dir: Path) -> Path | None:
    """已存在的输出 EPUB:优先新命名,兼容旧版本的下划线命名。

    旧版(≤v0.2.1)输出的文件名把空格 sanitize 成下划线;升级后若只认新名,
    已完成的输出会被当成未完成而重复转换,所以跳过判断要认两种命名。
    """
    for p in (epub_path(source, output_dir),
              output_dir / f"{sanitize_name(source.stem)}.epub"):
        if p.exists():
            return p
    return None


def parse_title_author(stem: str) -> tuple[str, str | None]:
    """解析文件名中的「标题 - 作者」模式。
    匹配形如 "马克思主义与性少数解放 - 瑞士红星党" 的文件名:
    - 以「空格-空格」分隔,前后两段
    - 返回 (标题, 作者);不匹配时返回 (原文件名, None)
    """
    m = re.match(r"^(.*?)\s+-\s+(.+)$", stem)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        author = re.sub(r"\s+", " ", m.group(2)).strip()
        if len(title) >= 2 and author:
            return title, author
    return stem, None


@dataclass
class TaskResult:
    source: Path
    status: str = "pending"   # done / skipped / failed / pending
    backend: str = ""
    pdf_type: str = ""
    error: str = ""
    elapsed: float = 0.0
    epub: Path | None = None
    verify: str = ""          # 结构校验摘要,如 "0 失败 0 警告"


def iter_sources(paths: list[Path]) -> list[Path]:
    """展开输入:文件直接收,目录递归收集支持的扩展名。"""
    sources: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES:
                    sources.append(f)
        elif p.is_file():
            sources.append(p)
        else:
            logger.warning("路径不存在,跳过: %s", p)
    return sources


def is_done(source: Path, output_dir: Path, force: bool = False) -> bool:
    """EPUB 已存在且不早于源文件 → 视为已完成。"""
    if force:
        return False
    epub = existing_epub(source, output_dir)
    if epub is None or epub.stat().st_size == 0:
        return False
    return epub.stat().st_mtime >= source.stat().st_mtime


def verify_output(epub: Path, *, strict: bool = False) -> VerifyResult:
    """EPUB 结构校验(idea.md §12),接入转换流程。

    默认只告警(不打断既有流程);strict=True 时只要有 error 就抛 VerifyError,
    使该任务判 failed。
    """
    result = verify_epub(epub)
    for issue in result.issues:
        log = logger.warning if issue.level == "warning" else logger.error
        log("[verify] %s", issue.message)
    logger.info("[verify] %s", result.summary())
    if strict and result.errors:
        raise VerifyError(
            f"EPUB 校验未通过({result.summary()}): {result.errors[0].message}"
        )
    return result


def process_one(
    source: Path,
    *,
    config: dict,
    work_root: Path,
    output_dir: Path,
    backend_override: str | None = None,
    retries: int = 2,
    force: bool = False,
    clean_options: CleanOptions | None = None,
    strict: bool = False,
) -> TaskResult:
    """处理单个文件(PDF 或 Markdown),带重试与跳过。"""
    t0 = time.time()
    result = TaskResult(source=source)
    safe_stem = sanitize_name(source.stem)   # 工作目录名(PyMuPDF 限制:不能含空格)
    out_epub = epub_path(source, output_dir)  # 输出 EPUB(保留原文空格)

    if is_done(source, output_dir, force):
        result.status = "skipped"
        result.epub = existing_epub(source, output_dir) or out_epub
        result.elapsed = time.time() - t0
        logger.info("跳过(已完成): %s", source.name)
        return result

    attempt = 0
    last_error: Exception | None = None
    while attempt <= retries:
        attempt += 1
        try:
            if source.suffix.lower() == ".md":
                result.backend, result.pdf_type = "markdown", "markdown"
                result = _process_markdown(source, work_root, output_dir, t0, result,
                                           safe_stem, out_epub.stem, clean_options, strict)
            else:
                result = _process_pdf(source, config, work_root, output_dir,
                                      backend_override, t0, result, safe_stem, out_epub.stem,
                                      clean_options, strict)
            result.status = "done"
            return result
        except VerifyError as e:
            # 校验失败是确定性结果:重试只会重复整条链路(含云端 OCR 额度),直接判失败
            last_error = e
            break
        except Exception as e:  # noqa: BLE001 - 批处理需兜住所有失败
            last_error = e
            logger.warning("第 %d 次尝试失败(%s): %s", attempt, source.name, e)
            if attempt <= retries:
                sleep = 2 ** attempt
                logger.info("  %ds 后重试...", sleep)
                time.sleep(sleep)

    result.status = "failed"
    result.error = str(last_error)
    logger.error("失败: %s → %s", source.name, last_error)
    return result


def _process_pdf(source, config, work_root, output_dir, backend_override, t0, result,
                 safe_stem, out_name, clean_options=None, strict=False) -> TaskResult:
    work = work_root / safe_stem
    if backend_override and backend_override != "auto":
        backend = get_backend(backend_override, config.get(backend_override, {}))
        conv = backend.convert(source, work)
        result.backend = conv.backend
        result.pdf_type = "manual"
    else:
        conv, detection = convert_auto(source, work, config=config)
        result.backend = conv.backend
        result.pdf_type = detection.pdf_type.value
        if getattr(detection, "suspicious_pages", 0) > 0:
            logger.warning(
                "⚠️ %s: %d/%d 页疑似文字层损坏(乱码),本地提取可能不可读;"
                "如需 OCR 请用 --backend mineru/paddleocr 重跑",
                source.name, detection.suspicious_pages, detection.total_pages,
            )

    # Markdown 清理(按 clean_options 开关)
    report = clean_file(conv.book_md, options=clean_options)
    for issue in report.issues:
        logger.info("[clean] %s", issue)

    title, author = parse_title_author(source.stem)
    epub = build_epub(conv.book_md, work, output_dir, title=title, author=author, out_name=out_name)
    result.epub = epub
    result.verify = verify_output(epub, strict=strict).summary()
    result.elapsed = time.time() - t0
    logger.info("完成(%s/%s): %s → %s", result.pdf_type, result.backend, source.name, epub.name)
    return result


def _process_markdown(source, work_root, output_dir, t0, result, safe_stem, out_name,
                      clean_options=None, strict=False) -> TaskResult:
    work = work_root / safe_stem
    work.mkdir(parents=True, exist_ok=True)
    # 已有 Markdown:复制到统一 work 目录(连同 images/)
    book_md = work / "book.md"
    book_md.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    src_images = source.parent / "images"
    if src_images.is_dir():
        import shutil

        dst_images = work / "images"
        dst_images.mkdir(parents=True, exist_ok=True)
        for img in src_images.iterdir():
            if img.is_file():
                shutil.copy2(img, dst_images / img.name)

    report = clean_file(book_md, options=clean_options)
    for issue in report.issues:
        logger.info("[clean] %s", issue)
    title, author = parse_title_author(source.stem)
    epub = build_epub(book_md, work, output_dir, title=title, author=author, out_name=out_name)
    result.epub = epub
    result.verify = verify_output(epub, strict=strict).summary()
    result.elapsed = time.time() - t0
    logger.info("完成(markdown): %s → %s", source.name, epub.name)
    return result


def process_batch(
    paths: list[Path],
    *,
    config: dict | None = None,
    config_path: Path | None = None,
    work_root: str | Path = "work",
    output_dir: str | Path = "output",
    backend_override: str | None = None,
    retries: int = 2,
    force: bool = False,
    clean_options: CleanOptions | None = None,
    strict: bool = False,
) -> list[TaskResult]:
    """批量处理,返回全部任务结果。单个失败不中断。

    clean_options=None → 按 config 的 `clean:` 段(CleanOptions 默认值)。
    传入的开关会先落到 config(如关闭 bold 等价于 pymupdf.bold_fonts 为空),
    再逐文件生效。

    strict=True → 每个 EPUB 生成后做结构校验,有 error 即判该任务 failed
    (默认 False:只告警,不打断既有流程)。
    """
    if config is None:
        config = load_config(config_path or config_file())
    if clean_options is None:
        clean_options = resolve_options(config)
    clean_options.apply(config)

    work_root = Path(work_root)
    output_dir = Path(output_dir)
    work_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    sources = iter_sources(paths)
    if not sources:
        logger.warning("没有找到可处理的文件")
        return []

    logger.info("共 %d 个文件待处理", len(sources))
    logger.debug("清理项: %s", clean_options)
    results: list[TaskResult] = []
    for src in sources:
        results.append(process_one(
            src,
            config=config,
            work_root=work_root,
            output_dir=output_dir,
            backend_override=backend_override,
            retries=retries,
            force=force,
            clean_options=clean_options,
            strict=strict,
        ))

    # 汇总
    done = sum(1 for r in results if r.status == "done")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    total_s = sum(r.elapsed for r in results)
    checked = [r for r in results if r.verify]
    if checked:
        # 校验摘要:有 error 的条目单独列出(便于 --strict 时定位)
        bad = [r for r in checked if not r.verify.startswith("0 失败")]
        logger.info("结构校验: %d 个 EPUB, %d 个存在失败项", len(checked), len(bad))
        for r in bad:
            logger.warning("  ⚠ %s: %s", r.source.name, r.verify)
    logger.info("批处理完成: 共 %d, 成功 %d, 跳过 %d, 失败 %d, 总耗时 %.1fs",
                len(results), done, skipped, failed, total_s)
    return results


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    """配置日志:控制台 + 可选文件。"""
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(console)
    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handlers.append(fh)
    logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)
