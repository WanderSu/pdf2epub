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
    p.add_argument("--log", default=None, help="日志文件(默认 logs/batch-<时间>.log)")
    p.add_argument("--no-log", action="store_true", help="不写日志文件(仅控制台)")
    p.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.no_log:
        log_file = None
    else:
        log_file = args.log or (Path.cwd() / "logs" / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
    setup_logging(log_file=log_file, verbose=args.verbose)

    paths = [Path(p) for p in args.paths]
    results = process_batch(
        paths,
        work_root=args.work,
        output_dir=args.output,
        backend_override=None if args.backend == "auto" else args.backend,
        retries=args.retries,
        force=args.force,
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
