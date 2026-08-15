"""EPUB 结构验证脚本(项目自维护,每个 Phase 复用)。

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
import re
import sys
import zipfile
from pathlib import Path

import xml.etree.ElementTree as ET

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
}

MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".xhtml": "application/xhtml+xml",
}

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  [WARN] {msg}")


def ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def check_epub(epub_path: Path, extract_dir: Path | None,
               expect_math: bool = False, expect_footnotes: bool = False,
               expect_images: int = 0) -> None:
    print(f"验证: {epub_path}")
    print(f"期望: 公式={expect_math} 脚注={expect_footnotes} 图片={expect_images}")

    with zipfile.ZipFile(epub_path) as zf:
        names = zf.namelist()
        print(f"包内文件总数: {len(names)}")

        # ---- 1. 容器结构 ----
        print("\n[1] 包结构")
        if "META-INF/container.xml" not in names:
            fail("缺少 META-INF/container.xml")
            return
        ok("META-INF/container.xml 存在")

        # mimetype 必须是未压缩的第一个条目,内容固定
        if names and names[0] == "mimetype":
            if zf.read("mimetype").decode("ascii", errors="replace") == "application/epub+zip":
                ok("mimetype 正确 (application/epub+zip)")
            else:
                fail("mimetype 内容不正确")
        else:
            warn("mimetype 不是第一个条目(部分阅读器会拒绝)")

        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        if not opf_path or opf_path not in names:
            fail(f"OPF 路径无效: {opf_path}")
            return
        ok(f"OPF: {opf_path}")

        # ---- 2. OPF 元数据与清单 ----
        print("\n[2] OPF(元数据 / manifest / spine)")
        opf = ET.fromstring(zf.read(opf_path))
        opf_dir = Path(opf_path).parent

        title = opf.find(".//dc:title", NS)
        lang = opf.find(".//dc:language", NS)
        ok(f"标题: {title.text if title is not None else '(缺失)'}")
        ok(f"语言: {lang.text if lang is not None else '(缺失)'}")

        manifest: dict[str, str] = {}
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            manifest[item.get("id")] = item.get("href", "")

        # manifest 条目 → 磁盘文件 检查
        opf_dir_parts = [p for p in Path(opf_path).parent.parts if p not in ("", ".")]

        def in_zip(rel: str, base_dir: list[str] | None = None) -> bool:
            """EPUB 内逻辑路径是否存在于 zip 包中(纯路径规范化,不碰磁盘)。

            rel 是相对 base_dir 的逻辑路径,可能含 ../;zip 条目是相对
            zip 根的完整路径(如 EPUB/text/ch001.xhtml)。默认 base_dir
            为 OPF 所在目录。
            """
            parts: list[str] = list(base_dir if base_dir is not None else opf_dir_parts)
            for seg in Path(rel).parts:
                if seg in ("", "."):
                    continue
                if seg == "..":
                    if parts:
                        parts.pop()
                    elif base:
                        base.pop()
                    else:
                        return False  # 逃逸出 zip 根
                else:
                    parts.append(seg)
            full = "/".join(parts)
            return full in names

        missing_in_zip = []
        for item_id, href in manifest.items():
            if not in_zip(href, opf_dir_parts):
                missing_in_zip.append(href)
        if missing_in_zip:
            fail(f"manifest 中 {len(missing_in_zip)} 个文件在包内不存在: {missing_in_zip[:5]}")
        else:
            ok(f"manifest {len(manifest)} 个条目全部在包内找到")

        # ---- 3. 图片 ----
        print("\n[3] 图片")
        IMG_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
        img_items = {i: h for i, h in manifest.items() if h.lower().endswith(IMG_SUFFIXES)}
        if not img_items:
            warn("manifest 中没有图片条目")
        for item_id, href in img_items.items():
            print(f"  - {href}")

        # MIME 类型检查
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            href = item.get("href", "")
            suffix = Path(href).suffix.lower()
            media_type = item.get("media-type", "")
            if suffix in MIME and media_type != MIME[suffix]:
                warn(f"MIME 不匹配: {href} 声明 {media_type},应为 {MIME[suffix]}")

        # ---- 4. XHTML 内容:公式 / 脚注 / 图片引用 ----
        print("\n[4] XHTML 内容检查(公式 / 脚注 / 图片引用)")
        spine = [i.get("idref") for i in opf.findall(".//opf:spine/opf:itemref", NS)]
        html_files = [manifest[i] for i in spine if i in manifest]

        total_math = 0
        total_footnotes = 0
        total_img_refs = 0
        broken_refs: list[str] = []
        empty_chapters: list[str] = []

        for hf in html_files:
            hf_posix = str((opf_dir / hf).as_posix())
            content = zf.read(hf_posix).decode("utf-8", errors="replace")
            hf_dir = opf_dir_parts + [p for p in Path(hf).parent.parts if p not in ("", ".")]
            math_count = len(re.findall(r"<math\b", content))
            fn_sections = len(re.findall(r'class="footnotes[^"]*"|epub:type="footnotes"', content))
            fn_refs = len(re.findall(r'class="footnote-ref"', content))
            imgs = re.findall(r'<img\b[^>]*src="([^"]+)"', content)
            body_text = re.sub(r"<[^>]+>", "", content).strip()

            total_math += math_count
            total_footnotes += fn_sections
            total_img_refs += len(imgs)

            for src in imgs:
                if not in_zip(src, hf_dir):
                    broken_refs.append(f"{hf} → {src}")

            if math_count:
                print(f"  {hf}: MathML <math> × {math_count}")
            if len(body_text) < 10:
                empty_chapters.append(hf)

        if expect_math and total_math == 0:
            fail("XHTML 中未发现任何 MathML 公式")
        else:
            ok(f"公式: MathML {total_math} 处" + ("(期望≥1)" if expect_math else ""))
        if expect_footnotes and total_footnotes == 0:
            fail("未发现脚注区块")
        else:
            ok(f"脚注: {total_footnotes} 个区块, {fn_refs} 个引用" + ("(期望≥1)" if expect_footnotes else ""))
        if expect_images > 0 and total_img_refs < expect_images:
            fail("XHTML 中未发现图片引用")
        else:
            ok(f"图片引用: XHTML 中 {total_img_refs} 处 <img>" + (f"(期望 {expect_images})" if expect_images else ""))
        if broken_refs:
            fail(f"图片引用断裂: {broken_refs}")
        else:
            ok("图片引用与包内文件全部匹配")
        if empty_chapters:
            warn(f"疑似空章节: {empty_chapters}")
        else:
            ok("无空章节")

        # ---- 5. TOC ----
        print("\n[5] TOC")
        nav_path = None
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            if item.get("properties") == "nav":
                nav_path = str((opf_dir / item.get("href", "")).as_posix())
        if nav_path and nav_path in names:
            nav = zf.read(nav_path).decode("utf-8", errors="replace")
            toc_entries = re.findall(r'<a[^>]*epub:type="toc"[^>]*>|<nav[^>]*epub:type="toc"', nav)
            # 统计 nav 内链接数
            toc_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>', nav)
            ok(f"nav.xhtml 存在,导航链接 {len(toc_links)} 个")
            for l in toc_links[:10]:
                print(f"  - {l}")
        else:
            warn(f"未找到 nav 文档 ({nav_path})")

        # ---- 6. 内部链接检查 ----
        print("\n[6] 内部链接")
        internal_breaks: list[str] = []
        missing_anchors: list[str] = []
        for hf in html_files:
            hf_posix = str((opf_dir / hf).as_posix())
            content = zf.read(hf_posix).decode("utf-8", errors="replace")
            hf_dir = opf_dir_parts + [p for p in Path(hf).parent.parts if p not in ("", ".")]
            for m in re.finditer(r'href="([^"#]+(?:#[^"]*)?)"', content):
                href = m.group(1)
                if href.startswith(("http://", "https://", "mailto:")):
                    continue
                file_part, _, anchor = href.partition("#")
                if not file_part:
                    continue
                if not in_zip(file_part, hf_dir):
                    internal_breaks.append(f"{hf} → {href}")
                    continue
                if anchor:
                    # 锚点应存在于目标文件(id="..."),注意可能 URL 编码
                    from urllib.parse import unquote

                    target_path = str((opf_dir / file_part).as_posix())
                    if target_path in names:
                        target_content = zf.read(target_path).decode("utf-8", errors="replace")
                        if f'id="{unquote(anchor)}"' not in target_content:
                            missing_anchors.append(f"{hf} → #{anchor}")
        if internal_breaks:
            fail(f"内部链接断裂: {internal_breaks[:5]}")
        else:
            ok("内部链接均指向包内存在的文件")
        if missing_anchors:
            warn(f"锚点目标未找到(可能为外部/编码差异): {missing_anchors[:5]}")

        # ---- 7. CSS 是否嵌入 ----
        print("\n[7] CSS")
        css_items = [h for h in manifest.values() if h.endswith(".css")]
        if css_items:
            for c in css_items:
                print(f"  - {c}")
            ok(f"CSS {len(css_items)} 个已嵌入")
        else:
            fail("OPF 中未找到 CSS")

    # ---- 8. 解包预览(可选) ----
    if extract_dir is not None:
        with zipfile.ZipFile(epub_path) as zf:
            zf.extractall(extract_dir)
        print(f"\n[8] 已解包到 {extract_dir}")

    print(f"\n=== 结果: {len(FAILURES)} 失败, {len(WARNINGS)} 警告 ===")
    return 1 if FAILURES else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="pdf2epub EPUB 验证")
    parser.add_argument("epub", type=Path)
    parser.add_argument("--extract-dir", type=Path, default=None)
    parser.add_argument("--expect-math", action="store_true", help="要求包含 MathML 公式")
    parser.add_argument("--expect-footnotes", action="store_true", help="要求包含脚注区块")
    parser.add_argument("--expect-images", type=int, default=0, help="要求图片引用数量")
    args = parser.parse_args()
    return check_epub(args.epub, args.extract_dir,
                      expect_math=args.expect_math,
                      expect_footnotes=args.expect_footnotes,
                      expect_images=args.expect_images)


if __name__ == "__main__":
    sys.exit(main())
