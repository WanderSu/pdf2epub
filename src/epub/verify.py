"""EPUB 结构校验(idea.md §12)。

纯逻辑模块:输入 EPUB 路径,输出结构化 issue 列表,不打印、不抛异常
(除 zip 无法打开外),供两处复用:

  - `scripts/verify_epub.py`:人工核验的 CLI(打印 [ OK ]/[WARN]/[FAIL])
  - `src/batch.py`:转换流程内的自动检查(默认告警,`--strict` 时判失败)

检查项:
1. 包结构:container.xml / mimetype / OPF 可解析
2. OPF 元数据(标题/语言)与 manifest ↔ 包内文件一一匹配
3. 图片:manifest 条目、MIME 类型、XHTML 中的 <img src> 是否可解析
4. XHTML 内容:MathML 公式 / 脚注区块 / 空章节
5. TOC:nav 文档与导航链接
6. 内部链接:href 指向的文件与锚点必须存在
7. CSS 是否嵌入
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

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

IMG_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

#: 空章节判定阈值(去掉标签后的正文字符数)
EMPTY_CHAPTER_CHARS = 10


@dataclass(frozen=True)
class VerifyIssue:
    """单条校验结果。level=error 表示硬失败(结构损坏),warning 表示可疑。"""

    level: str          # error / warning
    code: str           # 机器可读代码,如 image_missing / manifest_missing
    message: str


@dataclass
class VerifyResult:
    """校验结果:issues + 计数统计(供日志与报告使用)。"""

    issues: list[VerifyIssue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    readable: bool = True   # False = zip/OPF 本身读不了,后续检查已中止

    @property
    def errors(self) -> list[VerifyIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[VerifyIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        """无 error 即通过(warning 不阻断)。"""
        return self.readable and not self.errors

    def add(self, level: str, code: str, message: str) -> None:
        self.issues.append(VerifyIssue(level=level, code=code, message=message))

    def fail(self, code: str, message: str) -> None:
        self.add("error", code, message)

    def warn(self, code: str, message: str) -> None:
        self.add("warning", code, message)

    def summary(self) -> str:
        return f"{len(self.errors)} 失败 {len(self.warnings)} 警告"


def verify_epub(
    epub_path: str | Path,
    *,
    expect_math: bool = False,
    expect_footnotes: bool = False,
    expect_images: int = 0,
) -> VerifyResult:
    """校验 EPUB 结构,返回 VerifyResult(不打印任何内容)。

    expect_* 是调用方声明的期望(如「这本来是有公式的书」),不满足按失败计。
    """
    epub_path = Path(epub_path)
    result = VerifyResult()

    try:
        zf = zipfile.ZipFile(epub_path)
    except (zipfile.BadZipFile, OSError) as e:
        result.readable = False
        result.fail("archive_unreadable", f"无法打开 EPUB(zip 损坏或不存在): {e}")
        return result

    with zf:
        names = zf.namelist()
        result.stats["entries"] = len(names)

        # ---- 1. 包结构 ----
        if "META-INF/container.xml" not in names:
            result.fail("container_missing", "缺少 META-INF/container.xml")
            return result

        # mimetype 必须是第一个条目且内容固定(EPUB 规范要求不压缩存储)
        if names and names[0] == "mimetype":
            if zf.read("mimetype").decode("ascii", errors="replace") != "application/epub+zip":
                result.fail("mimetype_wrong", "mimetype 内容不正确(应为 application/epub+zip)")
        else:
            result.warn("mimetype_order", "mimetype 不是第一个条目(部分阅读器会拒绝)")

        try:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
        except ET.ParseError as e:
            result.fail("container_invalid", f"META-INF/container.xml 无法解析: {e}")
            return result
        rootfile = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        opf_path = rootfile.get("full-path") if rootfile is not None else None
        if not opf_path or opf_path not in names:
            result.fail("opf_missing", f"OPF 路径无效: {opf_path}")
            return result

        try:
            opf = ET.fromstring(zf.read(opf_path))
        except ET.ParseError as e:
            result.fail("opf_invalid", f"OPF 无法解析: {e}")
            return result

        opf_dir = Path(opf_path).parent
        opf_dir_parts = [p for p in opf_dir.parts if p not in ("", ".")]

        def in_zip(rel: str, base_dir: list[str] | None = None) -> bool:
            """EPUB 内逻辑路径是否存在于 zip 包中(纯路径规范化,不碰磁盘)。

            rel 相对 base_dir,可含 ../;zip 条目是相对 zip 根的完整路径。
            """
            parts: list[str] = list(base_dir if base_dir is not None else opf_dir_parts)
            for seg in Path(rel).parts:
                if seg in ("", "."):
                    continue
                if seg == "..":
                    if parts:
                        parts.pop()
                    else:
                        return False   # 逃逸出 zip 根
                else:
                    parts.append(seg)
            return "/".join(parts) in names

        # ---- 2. 元数据与 manifest ----
        title = opf.find(".//dc:title", NS)
        lang = opf.find(".//dc:language", NS)
        if title is None or not (title.text or "").strip():
            result.warn("title_missing", "OPF 缺少 dc:title")
        if lang is None or not (lang.text or "").strip():
            result.warn("language_missing", "OPF 缺少 dc:language")
        if opf.find(".//dc:identifier", NS) is None:
            result.warn("identifier_missing", "OPF 缺少 dc:identifier")

        manifest: dict[str, str] = {}
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            manifest[item.get("id")] = item.get("href", "")
        result.stats["manifest"] = len(manifest)

        missing_in_zip = [h for h in manifest.values() if not in_zip(h, opf_dir_parts)]
        if missing_in_zip:
            result.fail("manifest_missing",
                        f"manifest 中 {len(missing_in_zip)} 个文件在包内不存在: {missing_in_zip[:5]}")

        # ---- 3. 图片 ----
        img_items = {i: h for i, h in manifest.items() if h.lower().endswith(IMG_SUFFIXES)}
        result.stats["images"] = len(img_items)
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            href = item.get("href", "")
            suffix = Path(href).suffix.lower()
            media_type = item.get("media-type", "")
            if suffix in MIME and media_type != MIME[suffix]:
                result.warn("mime_mismatch", f"MIME 不匹配: {href} 声明 {media_type},应为 {MIME[suffix]}")

        # ---- 4. XHTML 内容:公式 / 脚注 / 图片引用 ----
        spine = [i.get("idref") for i in opf.findall(".//opf:spine/opf:itemref", NS)]
        html_files = [manifest[i] for i in spine if i in manifest]
        result.stats["chapters"] = len(html_files)

        total_math = 0
        total_footnote_sections = 0
        total_footnote_refs = 0
        total_img_refs = 0
        broken_refs: list[str] = []
        empty_chapters: list[str] = []

        for hf in html_files:
            hf_posix = str((opf_dir / hf).as_posix())
            try:
                content = zf.read(hf_posix).decode("utf-8", errors="replace")
            except KeyError:
                continue   # manifest 缺失已在上面报过
            hf_dir = opf_dir_parts + [p for p in Path(hf).parent.parts if p not in ("", ".")]
            math_count = len(re.findall(r"<math\b", content))
            fn_sections = len(re.findall(r'class="footnotes[^"]*"|epub:type="footnotes"', content))
            fn_refs = len(re.findall(r'class="footnote-ref"', content))
            imgs = re.findall(r'<img\b[^>]*src="([^"]+)"', content)
            body_text = re.sub(r"<[^>]+>", "", content).strip()

            total_math += math_count
            total_footnote_sections += fn_sections
            total_footnote_refs += fn_refs
            total_img_refs += len(imgs)

            for src in imgs:
                if not in_zip(src, hf_dir):
                    broken_refs.append(f"{hf} → {src}")
            if len(body_text) < EMPTY_CHAPTER_CHARS:
                empty_chapters.append(hf)

        result.stats["math"] = total_math
        result.stats["footnote_sections"] = total_footnote_sections
        result.stats["footnote_refs"] = total_footnote_refs
        result.stats["img_refs"] = total_img_refs

        if broken_refs:
            result.fail("image_missing", f"图片引用缺失: {broken_refs[:5]}")
        if expect_math and total_math == 0:
            result.fail("math_expected", "XHTML 中未发现任何 MathML 公式(期望 ≥1)")
        if expect_footnotes and total_footnote_sections == 0:
            result.fail("footnotes_expected", "未发现脚注区块(期望 ≥1)")
        if expect_images > 0 and total_img_refs < expect_images:
            result.fail("images_expected",
                        f"图片引用不足: 实际 {total_img_refs} 处 <img>,期望 {expect_images}")
        if empty_chapters:
            result.warn("empty_chapters", f"疑似空章节: {empty_chapters[:5]}")

        # ---- 5. TOC ----
        nav_path = None
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            if item.get("properties") == "nav":
                nav_path = str((opf_dir / item.get("href", "")).as_posix())
        if nav_path and nav_path in names:
            nav = zf.read(nav_path).decode("utf-8", errors="replace")
            toc_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>', nav)
            result.stats["toc_links"] = len(toc_links)
        else:
            result.warn("nav_missing", f"未找到 nav 文档 ({nav_path})")

        # ---- 6. 内部链接 ----
        internal_breaks: list[str] = []
        missing_anchors: list[str] = []
        for hf in html_files:
            hf_posix = str((opf_dir / hf).as_posix())
            try:
                content = zf.read(hf_posix).decode("utf-8", errors="replace")
            except KeyError:
                continue
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
                    target_path = str((opf_dir / file_part).as_posix())
                    if target_path in names:
                        target_content = zf.read(target_path).decode("utf-8", errors="replace")
                        if f'id="{unquote(anchor)}"' not in target_content:
                            missing_anchors.append(f"{hf} → #{anchor}")
        if internal_breaks:
            result.fail("link_broken", f"内部链接断裂: {internal_breaks[:5]}")
        if missing_anchors:
            result.warn("anchor_missing", f"锚点目标未找到(可能为外部/编码差异): {missing_anchors[:5]}")

        # ---- 7. CSS ----
        # 只认「manifest 声明且文件确实在包内」的 CSS;声明了却缺失 = 排版失效
        css_items = [h for h in manifest.values()
                     if h.endswith(".css") and in_zip(h, opf_dir_parts)]
        result.stats["css"] = len(css_items)
        if not css_items:
            result.fail("css_missing", "OPF 中未找到可用的 CSS(排版样式缺失)")

    return result
