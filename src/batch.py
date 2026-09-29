"""批处理核心(idea.md §15 / Phase 7)。

能力:
  - 批量处理(文件或目录,串行执行,优先稳定性)
  - 日志(控制台 + 文件)
  - 失败重试(指数退避)
  - 跳过已完成文件(EPUB 存在且不早于源文件)
  - 断点续跑(重跑时自动跳过已完成)
  - 单个文件失败不中断整体

状态判定:output/<name>.epub 存在、容器写完整、且 mtime ≥ 源文件 mtime → 已完成
(只看「存在 + 非空 + 更新」会把截断/写坏的产物判成完成,见 is_done)。
失败产物会被移出 output/(改名加 .failed),避免下一次运行把它当成已完成而跳过。
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backends import get_backend
from backends.base import file_fingerprint, is_retryable
from convert import convert_auto, load_config
from epub.content import verify_content
from epub.pandoc import MISSING_PANDOC_HINT, PandocMissingError, build_epub, pandoc_available
from epub.verify import VerifyResult, archive_issue, verify_epub
from lang_detect import resolve_language
from markdown.cleaner import CleanOptions, clean_file, resolve_options
from detector.pdf_detector import PDFType
from paths import config_file
import events

#: 封面 JPEG 宽度 / 质量(v0.3.2 P1-4:PDF 首页渲染,不裁切)
COVER_WIDTH = 1200
COVER_QUALITY = 88
#: 语言检测读取的正文前多少字符(整本读没必要,前几万字足够定脚本)
LANG_SAMPLE_CHARS = 20000
#: 失败产物的改名后缀:失败产物要移出「完成判定」的视野,但保留现场供排查
FAILED_OUTPUT_SUFFIX = ".failed"


def stable_identifier(source: Path, epub_stem: str) -> str:
    """由**源文件内容指纹**派生的稳定 dc:identifier。

    同一本书重复转换必须得到同一个 id(否则阅读器会把它当新书,书架重复、
    阅读进度丢失);内容变了才换 id。所以用 uuid5(内容指纹)而不是随机 uuid4。
    """
    digest = file_fingerprint(source)
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'pdf2epub:{epub_stem}:{digest}')}"


def source_date(source: Path) -> str:
    """dc:date:优先用 PDF 内嵌的创建日期,取不到就用源文件修改日期(ISO)。"""
    if source.suffix.lower() == ".pdf":
        try:
            import pymupdf

            with pymupdf.open(source) as doc:
                raw = (doc.metadata or {}).get("creationDate", "")
            parsed = _parse_pdf_date(raw)
            if parsed:
                return parsed
        except Exception:  # noqa: BLE001 - 元数据读不到不是失败理由
            pass
    return datetime.fromtimestamp(source.stat().st_mtime).date().isoformat()


def _parse_pdf_date(raw: str) -> str | None:
    """PDF 的 `D:20200101120000+08'00'` → `2020-01-01`。"""
    m = re.match(r"D:(\d{4})(\d{2})?(\d{2})?", raw or "")
    if not m or not m.group(2) or not m.group(3):
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def render_cover(pdf: Path, work_dir: Path, *, width: int = COVER_WIDTH,
                 quality: int = COVER_QUALITY) -> Path | None:
    """把 PDF 首页渲染成封面 JPEG(idea.md §12:封面用 PDF 第一页)。

    返回 None 表示这次没做成封面(渲染失败 / 空 PDF)—— 不阻断转换,EPUB 只是没封面。
    """
    try:
        import pymupdf

        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf) as doc:
            if doc.page_count == 0:
                return None
            page = doc[0]
            zoom = width / max(1.0, page.rect.width)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            out = work_dir / "cover.jpg"
            pix.save(str(out), jpg_quality=quality)
        return out if out.exists() and out.stat().st_size else None
    except Exception as e:  # noqa: BLE001 - 封面失败不影响正文
        logger.warning("封面渲染失败(%s): %s", pdf.name, e)
        return None


def epub_language(book_md: Path, override: str | None) -> str:
    """EPUB dc:language:显式覆盖 > 正文脚本检测 > zh-CN。"""
    try:
        text = book_md.read_text(encoding="utf-8", errors="replace")[:LANG_SAMPLE_CHARS]
    except OSError:
        text = ""
    return resolve_language(text, override=override)

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


def source_identity(path: Path) -> str:
    """源文件身份键(Windows 路径大小写不敏感,比较前统一规范化)。"""
    return os.path.normcase(str(Path(path).resolve()))


def name_conflicts(sources: list[Path]) -> dict[str, list[Path]]:
    """同一批输入里**会写进同一个 work/ 与 output/ 路径**的源文件。

    两个不同的源文件撞名(工作目录名或输出 EPUB 名相同)时,先转的那个会占住
    `work/<名>/` 与 `output/<名>.epub`,后一个随即被 `is_done()` 判成「已完成」而
    静默跳过 —— 用户以为两本都转了,交付物却少一本(工作目录还会串用上一本的图片)。
    撞名时不猜谁该赢、也不覆盖:显式列出冲突,由 process_batch 判失败。

    返回 ``{源文件身份键: [与之撞名的其它源文件]}``;同一条路径重复传入不算冲突。
    """
    def work_key(p: Path) -> str:
        return f"work:{sanitize_name(p.stem)}"

    def out_key(p: Path) -> str:
        return f"out:{output_stem(p.stem)}"

    conflicts: dict[str, list[Path]] = {}
    for key_of in (work_key, out_key):          # 工作目录名与输出名各自判重
        groups: dict[str, list[Path]] = {}
        for src in sources:
            groups.setdefault(key_of(src), []).append(src)
        for group in groups.values():
            if len({source_identity(p) for p in group}) < 2:
                continue
            for src in group:
                me = source_identity(src)
                others = conflicts.setdefault(me, [])
                others.extend(p for p in group
                              if source_identity(p) != me and p not in others)
    return conflicts


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
    """EPUB 已存在、**容器写完整**且不早于源文件 → 视为已完成。

    只比「文件存在 + 大小 > 0 + mtime ≥ 源」是不够的:被截断或写坏的 EPUB 同样
    「非空且比源新」,于是被打上「已完成」永久跳过 —— 交付物打不开,用户还无从发现
    (重跑也会跳过)。所以完成判定必须先把容器完整性验一遍,不完整就重转。
    """
    if force:
        return False
    epub = existing_epub(source, output_dir)
    if epub is None:
        return False
    issue = archive_issue(epub)          # 0 字节、截断、数据区损坏都在这里被挡下
    if issue is not None:
        logger.warning("已有产物不完整(%s),将重新转换: %s", issue, epub.name)
        events.emit("warning", code="output_incomplete", message=issue,
                    file=source.name, epub=epub.name)
        return False
    return epub.stat().st_mtime >= source.stat().st_mtime


def stat_signature(path: Path) -> tuple[int, float] | None:
    """(大小, mtime) 快照;文件不存在时返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime)


def discard_failed_output(epub: Path, before: tuple[int, float] | None) -> Path | None:
    """把**本次运行写出的失败产物**移出 output/,免得下次被 is_done() 判成「已完成」。

    失败结果不能伪装成成功结果:`--strict` 判失败时(pandoc 只写了一半时同理)产物
    已经在 output/ 里 —— 非空、mtime 比源新,下一次运行会直接跳过它,用户既看不到
    失败,也没法重转。

    只有在文件确实是**本次运行**写出的(大小/mtime 与运行前不同)时才动它:`--force`
    重转时若失败发生在构建之前,output 里放的是上一轮**成功**的成果,不能动它。

    改名不删除(现场留着排查);改名失败时退而删除 —— 总之不能把它留在那个位置上。
    """
    after = stat_signature(epub)
    if after is None or after == before:
        return None
    target = epub.with_name(epub.name + FAILED_OUTPUT_SUFFIX)
    try:
        os.replace(epub, target)
    except OSError as e:
        logger.warning("失败产物改名失败(%s),改为删除以免被当成已完成", e)
        try:
            epub.unlink()
        except OSError as e2:
            logger.error("失败产物既移不走也删不掉(%s): %s 可能被下次运行判成已完成",
                         e2, epub)
        return None
    logger.warning("失败产物已移到 %s(不会被当成已完成)", target.name)
    return target


def _fail(result: TaskResult, source: Path, error: Exception, *, code: str,
          out_epub: Path, before_output: tuple[int, float] | None) -> TaskResult:
    """统一的失败收尾:标记失败 → 移走本次的失败产物 → 发事件 + 日志。"""
    result.status = "failed"
    result.error = str(error)
    result.epub = None                    # 失败产物不是交付物
    quarantined = discard_failed_output(out_epub, before_output)
    if quarantined is not None:
        result.error += f"(失败产物已移到 {quarantined.name},不会被当成已完成)"
    events.emit("error", code=events.error_code(error, default=code), message=str(error),
                file=source.name)
    logger.error("失败: %s → %s", source.name, result.error)
    return result


def verify_output(epub: Path, *, strict: bool = False,
                  book_md: Path | None = None,
                  expected_pages: int | None = None) -> VerifyResult:
    """EPUB 结构与内容完整性校验(idea.md §12 / v0.3.2 P0-3),接入转换流程。

    - 结构:容器 / manifest / 链接 / 图片 / 公式 / CSS(verify_epub)
    - 内容:对照源 Markdown 检查有无内容丢失(book_md 非空时追加)

    默认只告警(不打断既有流程);strict=True 时只要有 error 就抛 VerifyError,
    使该任务判 failed。
    """
    result = verify_epub(epub)
    if book_md is not None and Path(book_md).exists():
        content = verify_content(book_md, epub, expected_pages=expected_pages)
        result.issues.extend(content.issues)
        result.stats.update(content.stats)
    for issue in result.issues:
        log = logger.warning if issue.level == "warning" else logger.error
        log("[verify] %s", issue.message)
    logger.info("[verify] %s", result.summary())
    if strict and result.errors:
        raise VerifyError(
            f"EPUB 校验未通过({result.summary()}): {result.errors[0].message}"
        )
    return result


def _conflict_result(source: Path, others: list[Path]) -> TaskResult:
    """重名冲突的失败结果(不转换、不覆盖,并且**不静默**)。"""
    result = TaskResult(source=source, status="failed")
    result.error = (
        f"源文件重名冲突:与 {'、'.join(sorted(p.name for p in others))} 解析出相同的"
        f"输出文件名/工作目录(会互相覆盖产物,并被误判为「已完成」而静默跳过)"
        f" —— 请重命名其中一个,或分开转换"
    )
    logger.error("失败: %s → %s", source.name, result.error)
    events.emit("error", code="name_conflict", message=result.error, file=source.name)
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
    lang_override: str | None = None,
) -> TaskResult:
    """处理单个文件(PDF 或 Markdown),带重试与跳过。

    lang_override: 显式指定的 EPUB 语言(CLI `--lang` / 配置),优先于正文检测。
    """
    t0 = time.time()
    result = TaskResult(source=source)
    safe_stem = sanitize_name(source.stem)   # 工作目录名(PyMuPDF 限制:不能含空格)
    out_epub = epub_path(source, output_dir)  # 输出 EPUB(保留原文空格)

    if is_done(source, output_dir, force):
        result.status = "skipped"
        result.epub = existing_epub(source, output_dir) or out_epub
        result.elapsed = time.time() - t0
        logger.info("跳过(已完成): %s", source.name)
        events.emit("skip", file=source.name, epub=result.epub.name)
        return result

    # 失败时用它判断 out_epub 是不是本次运行写出来的(见 discard_failed_output)
    before_output = stat_signature(out_epub)

    # 环境缺失(未安装 pandoc)必须在昂贵阶段之前暴露:否则提取(含云端 OCR)全跑完
    # 才发现生成不了 EPUB,重跑还得再花一次额度。
    if not pandoc_available():
        return _fail(
            result, source,
            PandocMissingError(f"{MISSING_PANDOC_HINT};本次未执行提取与 OCR"),
            code="missing_dependency", out_epub=out_epub, before_output=before_output,
        )

    attempt = 0
    last_error: Exception | None = None
    while attempt <= retries:
        attempt += 1
        try:
            if source.suffix.lower() == ".md":
                result.backend, result.pdf_type = "markdown", "markdown"
                result = _process_markdown(source, work_root, output_dir, t0, result,
                                           safe_stem, out_epub.stem, clean_options, strict,
                                           lang_override)
            else:
                result = _process_pdf(source, config, work_root, output_dir,
                                      backend_override, t0, result, safe_stem, out_epub.stem,
                                      clean_options, strict, lang_override)
            result.status = "done"
            return result
        except VerifyError as e:
            # 校验失败是确定性结果:重试只会重复整条链路(含云端 OCR 额度),直接判失败
            last_error = e
            break
        except PandocMissingError as e:
            # 环境缺失(如运行中 PATH 变了)同样是确定性失败,重试只是再撞一次墙
            last_error = e
            break
        except Exception as e:  # noqa: BLE001 - 批处理需兜住所有失败
            last_error = e
            # 后端会明确标注「确定性失败」(认证/参数/配额/输入文件错误/云端任务判死):
            # 这类错误重试改变不了结果,扫描书还会重复上传、重复消耗额度
            if not is_retryable(e):
                logger.error("不可重试的失败,不再重试: %s", e)
                break
            logger.warning("第 %d 次尝试失败(%s): %s", attempt, source.name, e)
            if attempt <= retries:
                sleep = 2 ** attempt
                # 重试对用户可见(否则「卡住不动」与「正在重试」在界面上无法区分)
                events.emit("retry", attempt=attempt, retries=retries, wait=sleep,
                            message=str(e), file=source.name)
                logger.info("  %ds 后重试...", sleep)
                time.sleep(sleep)

    code = ("verify_failed" if isinstance(last_error, VerifyError)
            else "missing_dependency" if isinstance(last_error, PandocMissingError)
            else "convert_failed")
    return _fail(result, source, last_error, code=code, out_epub=out_epub,
                 before_output=before_output)


def _process_pdf(source, config, work_root, output_dir, backend_override, t0, result,
                 safe_stem, out_name, clean_options=None, strict=False,
                 lang_override=None) -> TaskResult:
    work = work_root / safe_stem
    detection = None                      # 手动指定后端时没有检测结果
    if backend_override and backend_override != "auto":
        events.stage("detect", "done", skipped=True)
        events.stage("extract", "start", backend=backend_override)
        backend = get_backend(backend_override, config.get(backend_override, {}))
        conv = backend.convert(source, work)
        result.backend = conv.backend
        result.pdf_type = "manual"
        events.stage("extract", "done", backend=conv.backend)
    else:
        # detect / extract 阶段事件由 convert_auto 负责(检测与提取都在里面)
        conv, detection = convert_auto(source, work, config=config)
        result.backend = conv.backend
        result.pdf_type = detection.pdf_type.value
        if getattr(detection, "suspicious_pages", 0) > 0:
            logger.warning(
                "⚠️ %s: %d/%d 页疑似文字层损坏(乱码),本地提取可能不可读;"
                "如需 OCR 请用 --backend mineru/paddleocr 重跑",
                source.name, detection.suspicious_pages, detection.total_pages,
            )
            events.emit("warning", code="suspicious_pages",
                        message=f"{detection.suspicious_pages}/{detection.total_pages} 页疑似文字层损坏",
                        pages=detection.total_pages, suspicious_pages=detection.suspicious_pages)

    # Markdown 清理(按 clean_options 开关)
    events.stage("clean", "start")
    report = clean_file(conv.book_md, options=clean_options)
    for issue in report.issues:
        logger.info("[clean] %s", issue)
    events.stage("clean", "done", issues=len(report.issues))

    events.stage("build", "start")
    title, author = parse_title_author(source.stem)
    cover = render_cover(source, work)
    epub = build_epub(conv.book_md, work, output_dir, title=title, author=author, out_name=out_name,
                      lang=epub_language(conv.book_md, lang_override),
                      identifier=stable_identifier(source, out_name),
                      date=source_date(source), cover_image=cover)
    events.stage("build", "done", cover=bool(cover))
    result.epub = epub
    events.stage("verify", "start")
    verify = verify_output(
        epub, strict=strict, book_md=conv.book_md,
        expected_pages=getattr(detection, "total_pages", None) or conv.stats.get("pages"),
    )
    result.verify = verify.summary()
    events.emit("verify", errors=len(verify.errors), warnings=len(verify.warnings),
                message=verify.errors[0].message if verify.errors else
                        (verify.warnings[0].message if verify.warnings else ""))
    events.stage("verify", "done")
    result.elapsed = time.time() - t0
    events.emit("complete", file=source.name, epub=epub.name, backend=result.backend,
                pdf_type=result.pdf_type, verify=result.verify,
                seconds=round(result.elapsed, 2))
    logger.info("完成(%s/%s): %s → %s", result.pdf_type, result.backend, source.name, epub.name)
    return result


def _process_markdown(source, work_root, output_dir, t0, result, safe_stem, out_name,
                      clean_options=None, strict=False, lang_override=None) -> TaskResult:
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

    # Markdown 输入没有「提取」这一步(只是复制),extract 直接以 skipped 收尾
    events.stage("extract", "done", backend="markdown", skipped=True)
    events.stage("clean", "start")
    report = clean_file(book_md, options=clean_options)
    for issue in report.issues:
        logger.info("[clean] %s", issue)
    events.stage("clean", "done", issues=len(report.issues))
    events.stage("build", "start")
    title, author = parse_title_author(source.stem)
    epub = build_epub(book_md, work, output_dir, title=title, author=author, out_name=out_name,
                      lang=epub_language(book_md, lang_override),
                      identifier=stable_identifier(source, out_name),
                      date=source_date(source))
    events.stage("build", "done", cover=False)
    result.epub = epub
    events.stage("verify", "start")
    verify = verify_output(epub, strict=strict, book_md=book_md)
    result.verify = verify.summary()
    events.emit("verify", errors=len(verify.errors), warnings=len(verify.warnings),
                message=verify.errors[0].message if verify.errors else
                        (verify.warnings[0].message if verify.warnings else ""))
    events.stage("verify", "done")
    result.elapsed = time.time() - t0
    events.emit("complete", file=source.name, epub=epub.name, backend="markdown",
                pdf_type="markdown", verify=result.verify,
                seconds=round(result.elapsed, 2))
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
    lang: str | None = None,
) -> list[TaskResult]:
    """批量处理,返回全部任务结果。单个失败不中断。

    clean_options=None → 按 config 的 `clean:` 段(CleanOptions 默认值)。
    传入的开关会先落到 config(如关闭 bold 等价于 pymupdf.bold_fonts 为空),
    再逐文件生效。

    strict=True → 每个 EPUB 生成后做结构 + 内容校验,有 error 即判该任务 failed
    (默认 False:只告警,不打断既有流程)。

    lang → EPUB dc:language 的显式覆盖(CLI `--lang`);为 None 时用配置 `language:`
    再回退到正文脚本检测。

    同一批输入里两个源文件解析出相同的输出名/工作目录时,它们会互相覆盖产物,后一个
    还会被 is_done() 判成「已完成」而静默跳过(交付物悄悄少一本)—— 这种冲突一律判
    失败,见 name_conflicts。
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

    # 语言:CLI --lang > config `language:` > 正文脚本检测(逐文件)
    lang_override = lang or config.get("language") or None
    if lang_override:
        logger.info("EPUB 语言: %s(显式指定)", lang_override)

    sources = iter_sources(paths)
    if not sources:
        logger.warning("没有找到可处理的文件")
        return []

    logger.info("共 %d 个文件待处理", len(sources))
    logger.debug("清理项: %s", clean_options)
    conflicts = name_conflicts(sources)
    results: list[TaskResult] = []
    for src in sources:
        others = conflicts.get(source_identity(src))
        if others:                    # 撞名:明确失败,不转换、不覆盖、不静默跳过
            results.append(_conflict_result(src, others))
            continue
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
            lang_override=lang_override,
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
