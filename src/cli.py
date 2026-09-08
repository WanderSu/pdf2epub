"""ebook-converter CLI(idea.md §14 / Phase 7)。

用法:
    ebook-converter book.pdf
    ebook-converter book.pdf --backend pymupdf|mineru|paddleocr
    ebook-converter book.md
    ebook-converter ./books/            # 目录批量
    ebook-converter a.pdf b.md -o out/ --force --retries 2
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from batch import process_batch, setup_logging
from convert import load_config
from detector.pdf_detector import PDFDetector
from markdown.cleaner import CLEAN_KEYS, resolve_options
from paths import config_file


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ebook-converter",
        description="PDF/Markdown → EPUB 电子书转换工具",
    )
    p.add_argument("paths", nargs="+", help="PDF/Markdown 文件或目录(目录递归)")
    p.add_argument("--backend", choices=["auto", "pymupdf", "mineru", "paddleocr"],
                   default="auto", help="解析后端(默认 auto 自动检测)")
    p.add_argument("--output", "-o", default="output", help="EPUB 输出目录(默认 output/)")
    p.add_argument("--work", default="work", help="中间工作目录(默认 work/)")
    p.add_argument("--retries", type=int, default=2, help="单文件失败重试次数(默认 2)")
    p.add_argument("--force", action="store_true", help="强制重新处理(忽略已完成)")
    p.add_argument("--clean-disable", default=None,
                   help="关闭指定清理项(逗号分隔,可重复): "
                        "page_numbers,join_lines,cjk_spaces,bold,images")
    p.add_argument("--no-resume", action="store_true",
                   help="不复用云端 OCR 已提交的任务(默认中断后可续跑,不重新上传)")
    p.add_argument("--strict", action="store_true",
                   help="EPUB 生成后做结构校验,有失败项即判该文件失败(默认只告警)")
    p.add_argument("--log", default=None, help="日志文件(默认 logs/batch-<时间>.log)")
    p.add_argument("--no-log", action="store_true", help="不写日志文件(仅控制台)")
    p.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    return p


def main(argv: list[str] | None = None) -> int:
    # stdout 被桌面端管道捕获时默认块缓冲,进度日志不实时;强制行缓冲
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(argv)

    if args.no_log:
        log_file = None
    else:
        log_file = args.log or (Path.cwd() / "logs" / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
    setup_logging(log_file=log_file, verbose=args.verbose)

    paths = [Path(p) for p in args.paths]
    backend_override = None if args.backend == "auto" else args.backend

    # 清理开关:config 的 clean: 段为默认值,--clean-disable 叠加关闭
    try:
        config = load_config(config_file())
        clean_options = resolve_options(config, args.clean_disable)
    except ValueError as e:
        print(f"参数错误: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"配置缺失: {e}", file=sys.stderr)
        return 2
    if args.no_resume:
        for name in ("mineru", "paddleocr"):
            config.setdefault(name, {})["resume"] = False

    disabled = [k for k in CLEAN_KEYS if not getattr(clean_options, k)]
    if args.clean_disable and disabled:
        print(f"[clean] 已关闭清理项: {', '.join(disabled)}")

    # 交互询问:单文件 + auto + 交互终端 + 检测到疑似伪文字层(乱码)时,
    # 由用户手动决定是否改用云端 OCR。批处理/非交互模式不询问。
    if (
        backend_override is None
        and len(paths) == 1
        and paths[0].is_file()
        and paths[0].suffix.lower() == ".pdf"
        and sys.stdin.isatty()
    ):
        det = PDFDetector().detect(paths[0])
        if det.suspicious_pages > 0 and det.pdf_type.value != "scanned":
            print(
                f"⚠️ 检测到 {det.suspicious_pages}/{det.total_pages} 页疑似文字层损坏(乱码),"
                f"本地提取的文本可能不可读(当前类型: {det.pdf_type.value})。"
            )
            choice = input(
                "是否改用云端 OCR?\n"
                "  [1] MinerU\n"
                "  [2] PaddleOCR-VL\n"
                "  [3] 继续本地提取\n"
                "  [0] 取消\n"
                "请选择 [0-3]: "
            ).strip()
            if choice == "1":
                backend_override = "mineru"
            elif choice == "2":
                backend_override = "paddleocr"
            elif choice == "3":
                pass
            else:
                return 0  # 取消

    results = process_batch(
        paths,
        config=config,
        work_root=args.work,
        output_dir=args.output,
        backend_override=backend_override,
        retries=args.retries,
        force=args.force,
        clean_options=clean_options,
        strict=args.strict,
    )
    if not results:
        return 2

    failed = [r for r in results if r.status == "failed"]
    if failed:
        print(f"\n失败 {len(failed)} 个:")
        for r in failed:
            print(f"  ✗ {r.source.name}: {r.error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
