"""EPUB 结构验证 CLI(项目自维护,每个 Phase 复用)。

检查逻辑在 `src/epub/verify.py`(转换流程内也会调用),本脚本只负责
命令行入口与人类可读的报告输出。

验证项(对应 idea.md §12):
1. EPUB 包结构:container.xml / OPF / nav / 图片 / CSS
2. 图片:manifest 引用 ↔ 实际文件一一匹配,含中文文件名
3. 数学公式:MathML <math> 是否真正存在于 XHTML
4. 脚注:footnote 引用与回链
5. TOC:nav.xhtml 导航条目
6. 内部链接:指向本包内文件的 href 必须存在
7. 空章节检查(章节 XHTML 无正文)

用法: python scripts/verify_epub.py <book.epub> [--extract-dir <dir>]
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from epub.verify import VerifyResult, verify_epub   # noqa: E402

# 各段落归口的 issue 代码,用于「有则打印,无则打 OK」
SECTIONS = (
    ("[1] 包结构", ("container_missing", "container_invalid", "mimetype_wrong",
                    "mimetype_order", "opf_missing", "opf_invalid"),
     "META-INF/container.xml 与 OPF 正常"),
    ("[2] OPF(元数据 / manifest / spine)",
     ("title_missing", "language_missing", "identifier_missing", "manifest_missing"),
     "元数据完整,manifest 条目全部在包内找到"),
    ("[3] 图片", ("mime_mismatch",), "图片条目 MIME 类型正确"),
    ("[4] XHTML 内容检查(公式 / 脚注 / 图片引用)",
     ("math_expected", "footnotes_expected", "image_missing", "images_expected", "empty_chapters"),
     "公式 / 脚注 / 图片引用正常"),
    ("[5] TOC", ("nav_missing",), "nav.xhtml 存在"),
    ("[6] 内部链接", ("link_broken", "anchor_missing"), "内部链接均指向包内存在的文件"),
    ("[7] CSS", ("css_missing",), "CSS 已嵌入"),
)


def print_report(epub_path: Path, result: VerifyResult, extract_dir: Path | None = None) -> None:
    print(f"验证: {epub_path}")
    print(f"包内文件总数: {result.stats.get('entries', 0)}")
    if extract_dir is not None:
        print(f"解包目录: {extract_dir}")

    print(f"统计: 章节 {result.stats.get('chapters', 0)}, "
          f"图片条目 {result.stats.get('images', 0)}, "
          f"XHTML 内 <img> {result.stats.get('img_refs', 0)}, "
          f"MathML {result.stats.get('math', 0)}, "
          f"脚注区块 {result.stats.get('footnote_sections', 0)}, "
          f"导航链接 {result.stats.get('toc_links', 0)}")

    shown: set[str] = set()
    for title, codes, ok_text in SECTIONS:
        print(f"\n{title}")
        hits = [i for i in result.issues if i.code in codes]
        shown.update(codes)
        if not hits:
            print(f"  [ OK ] {ok_text}")
        for issue in hits:
            tag = "FAIL" if issue.level == "error" else "WARN"
            print(f"  [ {tag} ] {issue.message}")

    # 兜底:未归类的新 issue 也要看得见
    for issue in result.issues:
        if issue.code not in shown:
            tag = "FAIL" if issue.level == "error" else "WARN"
            print(f"  [ {tag} ] {issue.message}")

    if extract_dir is not None and result.readable:
        with zipfile.ZipFile(epub_path) as zf:
            zf.extractall(extract_dir)
        print(f"\n[8] 已解包到 {extract_dir}")

    print(f"\n=== 结果: {result.summary()} ===")


def main() -> int:
    parser = argparse.ArgumentParser(description="pdf2epub EPUB 验证")
    parser.add_argument("epub", type=Path)
    parser.add_argument("--extract-dir", type=Path, default=None)
    parser.add_argument("--expect-math", action="store_true", help="要求包含 MathML 公式")
    parser.add_argument("--expect-footnotes", action="store_true", help="要求包含脚注区块")
    parser.add_argument("--expect-images", type=int, default=0, help="要求图片引用数量")
    args = parser.parse_args()

    result = verify_epub(args.epub,
                         expect_math=args.expect_math,
                         expect_footnotes=args.expect_footnotes,
                         expect_images=args.expect_images)
    print_report(args.epub, result, args.extract_dir)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
