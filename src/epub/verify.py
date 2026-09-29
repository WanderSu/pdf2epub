"""EPUB 结构校验(idea.md §12)。

纯逻辑模块:输入 EPUB 路径,输出结构化 issue 列表,不打印、不抛异常
(除 zip 无法打开外),供两处复用:

  - `scripts/verify_epub.py`:人工核验的 CLI(打印 [ OK ]/[WARN]/[FAIL])
  - `src/batch.py`:转换流程内的自动检查(默认告警,`--strict` 时判失败)

检查项:
1. 包结构:container.xml / mimetype / OPF 可解析
2. OPF 元数据(标题/语言/日期格式)与 manifest ↔ 包内文件一一匹配、spine 引用有效
3. 图片:manifest 条目、MIME 类型、XHTML 中的 <img src> 是否可解析
4. XHTML 内容:**良构性**(XML 可解析)/ MathML 公式 / 脚注区块 / 空章节
5. TOC:nav 文档良构且链接目标存在、toc.ncx(EPUB2 阅读器的目录)指向的文件存在
6. 内部链接:href 指向的文件与锚点必须存在
7. CSS 是否嵌入、是否真的被正文引用(引用了不存在的样式文件同样报错)
8. 封面:cover-image 条目确实被封面页引用、封面页位于 spine 首位

另:`archive_issue()` 只查**容器是否写完整**(zip 可读 + mimetype + container.xml +
逐条目 CRC),供 `batch.is_done()` 判断「产物能不能算已完成」—— 它不做内容对照,
结论也不该被当成「这本书转得对」。
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
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
#: 「可疑偏短」章节阈值:仅统计,不告警 —— 版权页/前言/插页天然就短,
#: 拿它报警会让正常书每次都被提示(用户很快就会学会忽略提示)
SHORT_CHAPTER_CHARS = 200
#: 空章节占比达到此比例(且数量 ≥ EMPTY_CHAPTER_MIN) → 判定内容疑似丢失(error)
EMPTY_CHAPTER_RATIO = 0.5
EMPTY_CHAPTER_MIN = 3
#: 不参与「空/短章节」统计的文档类型(Pandoc 的封面页、标题页、目录)
FRONTMATTER_TYPES = ("frontmatter", "titlepage", "toc", "landmarks", "cover")
#: Pandoc 的封面页只有 `<body id="cover">` + `#cover-image`,**没有** epub:type ——
#: 不单独认它,每本带封面的书都会多一条「疑似空章节」告警
COVER_DOC_RE = re.compile(r'<body[^>]*id="cover"|<div[^>]*id="cover-image"')
#: dc:language 的宽松 BCP47 形状(zh-CN / en / ja / ko …)
LANG_RE = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")
#: dc:date 的宽松 ISO 8601 形状(Y / Y-M / Y-M-D,也接受 Pandoc 默认的完整时间戳)
DATE_RE = re.compile(
    r"^\d{4}(-\d{2}(-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?)?)?$"
)
#: 正文标题达到这个数量却没有 TOC 条目 → 报「目录为空」(单标题的书不报)
TOC_EMPTY_MIN_HEADINGS = 5


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


def archive_issue(epub_path: str | Path) -> str | None:
    """EPUB **容器级**检查:包写完整了就返回 None,否则返回一句人类可读的原因。

    用途是「完成状态判断」(batch.is_done):截断、空文件、数据区写坏或根本不是
    EPUB 的产物都不能算「已完成」—— 否则交付物打不开,而且因为「比源文件新」
    被永久跳过,用户连重转都做不到(除非自己删文件或加 `--force`)。

    只做 zip 层面的检查(mimetype + container.xml + 逐条目 CRC):判据要的是
    「这个包写完整了」,不是「这本书转得对」。刻意**不**复用 verify_epub —— 后者
    带内容对照与阈值(告警是常态),拿它的结论决定「跳过还是重转」会让正常书每次
    都被重转。
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names = set(zf.namelist())
            if "mimetype" not in names or "META-INF/container.xml" not in names:
                return "缺少 mimetype 或 META-INF/container.xml(不是完整的 EPUB 包)"
            if zf.read("mimetype").strip() != b"application/epub+zip":
                return "mimetype 内容不正确(不是完整的 EPUB 包)"
            # 逐条目 CRC 校验:被截断/写坏的数据区在这里暴露 —— 只看文件大小和
            # mtime 是发现不了的(大小正常、时间戳正常,内容却是坏的)。
            broken = zf.testzip()
            if broken:
                return f"包内条目损坏(CRC 校验失败): {broken}"
    # zlib.error 也要接:数据区被改坏时 testzip 会直接从解压器里抛出来,不是 BadZipFile
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
        return f"zip 不可读(损坏或被截断): {e}"
    except RuntimeError as e:            # 如条目被加密:完整性无从确认
        return f"zip 无法完整校验: {e}"
    return None


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

        # dc:language / dc:date 的**格式**(值本身合法与否,阅读器与书库解析依赖它)
        if lang is not None and (lang.text or "").strip() and not LANG_RE.match(lang.text.strip()):
            result.warn("language_invalid",
                        f"dc:language 不是合法的 BCP47 语言标签: {lang.text!r}")
        date = opf.find(".//dc:date", NS)
        if date is not None and (date.text or "").strip() and not DATE_RE.match(date.text.strip()):
            result.warn("date_invalid", f"dc:date 不是 ISO 日期: {date.text!r}")

        # ---- 2b. spine:阅读顺序必须指向真实存在的条目 ----
        # 以前只取「能在 manifest 里找到的」spine 项,找不到的**静默丢弃** ——
        # 章节会从阅读顺序里消失,而校验一切正常。
        spine_refs = [i.get("idref") for i in opf.findall(".//opf:spine/opf:itemref", NS)]
        if not spine_refs:
            result.fail("spine_empty", "OPF 的 spine 为空(阅读器没有任何可显示的内容)")
        unknown_spine = [r for r in spine_refs if r not in manifest]
        if unknown_spine:
            result.fail("spine_broken",
                        f"spine 引用了 manifest 中不存在的条目: {unknown_spine[:5]}"
                        "(这些章节会从阅读顺序里消失)")

        # ---- 2c. 封面:声明 + 引用 ----
        cover_base = ""
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            if "cover-image" in (item.get("properties") or "") or item.get("id") == "cover-image":
                cover_base = item.get("href", "").split("/")[-1]
        result.stats["cover"] = 1 if cover_base else 0

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
        # spine 的有效性已在 2b 校验过;这里取能在 manifest 里找到的文档读内容
        html_files = [manifest[i] for i in spine_refs if i in manifest]
        result.stats["chapters"] = len(html_files)
        # 书名标题(titles 章):pandoc 会把 Markdown 的首个 h1 单独变成一章,
        # 它只有书名、没有正文 —— 不排除的话**每本书**都会多一条「疑似空章节」告警
        book_title = re.sub(r"\s+", "", (title.text or "") if title is not None else "")

        total_math = 0
        total_footnote_sections = 0
        total_footnote_refs = 0
        total_img_refs = 0
        total_text_chars = 0
        total_headings = 0
        total_breaks = 0          # <br> 硬换行(诗行/图片文字块的换行靠它保住)
        total_tables = 0
        total_code_blocks = 0
        total_links = 0           # 内容里的链接(目录 / 标题页里的导航链接不算)
        total_internal_links = 0  # 包内链接(锚点/章节跳转)
        linked_css: set[str] = set()
        cover_doc_index: int | None = None
        broken_refs: list[str] = []
        empty_chapters: list[str] = []
        short_chapters: list[str] = []
        referenced_images: set[str] = set()
        # 标题层级分布(h1..h6):只看不判 —— 报告里用来核对层级是否成体系,
        # 不拿它告警(有些书的章节本来就跨级,报警会把正常书也说成有问题)
        heading_levels: dict[str, int] = {}

        for index, hf in enumerate(html_files):
            hf_posix = str((opf_dir / hf).as_posix())
            try:
                raw_doc = zf.read(hf_posix)
            except KeyError:
                continue   # manifest 缺失已在上面报过
            # **良构性**:EPUB 的正文是 XHTML(XML)。正则看不出未闭合标签 / 裸 `&` /
            # `<br>` 这类写法,而阅读器会整章拒绝渲染 —— 一个标签就能毁掉整本书。
            try:
                ET.fromstring(raw_doc)
            except ET.ParseError as e:
                result.fail("xhtml_invalid",
                            f"{hf} 不是良构 XHTML(阅读器可能拒绝渲染): {e}")
            content = raw_doc.decode("utf-8", errors="replace")
            hf_dir = opf_dir_parts + [p for p in Path(hf).parent.parts if p not in ("", ".")]
            # 标题页/目录/封面/标题章不是正文:它们天然短,参与空章节统计会一直误报;
            # 里面的链接是导航(long toc/landmarks),也不是「内容里的链接」。
            types = set(re.findall(r'epub:type="([^"]+)"', content))
            is_front = bool(types & set(FRONTMATTER_TYPES)) or bool(COVER_DOC_RE.search(content))
            math_count = len(re.findall(r"<math\b", content))
            fn_sections = len(re.findall(r'class="footnotes[^"]*"|epub:type="footnotes"', content))
            fn_refs = len(re.findall(r'class="footnote-ref"', content))
            imgs = re.findall(r'<img\b[^>]*src="([^"]+)"', content)
            breaks = len(re.findall(r"<br\b", content))
            tables = len(re.findall(r"<table\b", content))
            code_blocks = len(re.findall(r"<pre\b", content))
            # 正文文本必须**先剥掉 <head>**(pandoc 会把章节 id 写进 <title>,
            # 否则「只剩标题的空章节」会因为多出这十几个字符而逃过检测)
            body_html = re.sub(r"<head\b.*?</head>", "", content, flags=re.S)
            body_text = re.sub(r"<[^>]+>", "", body_html).strip()

            total_math += math_count
            total_footnote_sections += fn_sections
            total_footnote_refs += fn_refs
            total_img_refs += len(imgs)
            total_breaks += breaks
            total_tables += tables
            total_code_blocks += code_blocks
            total_text_chars += len(re.sub(r"\s+", "", body_text))
            level_counts = re.findall(r"<h([1-6])\b", content)
            total_headings += len(level_counts)
            for level in level_counts:
                heading_levels[f"headings_h{level}"] = \
                    heading_levels.get(f"headings_h{level}", 0) + 1
            for href in re.findall(r'<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"', content):
                linked_css.add(href.split("/")[-1])
            for href in re.findall(r'<a\b[^>]*href="([^"]+)"', content):
                if not href.startswith(("http://", "https://", "mailto:")):
                    total_internal_links += 1
                if not is_front:
                    total_links += 1      # 内容里的链接(目录/标题页的导航不算)

            for src in imgs:
                referenced_images.add(src.split("/")[-1])
                if not in_zip(src, hf_dir):
                    broken_refs.append(f"{hf} → {src}")
            # 图片也可能来自 <image xlink:href>(SVG 内嵌 / pandoc 的 svg 包装)
            for src in re.findall(r'<image\b[^>]*href="([^"]+)"', content):
                referenced_images.add(src.split("/")[-1])
            # 封面页 = 引用封面图的那份文档;它必须在 spine 首位,否则阅读器打开
            # 的第一页不是封面(书库缩略图与封面显示是两件事)
            if cover_base and cover_base in referenced_images and cover_doc_index is None:
                cover_doc_index = index
            if is_front:
                continue
            # 只有书名的标题章也不算(它本来就没有正文)
            if book_title and re.sub(r"\s+", "", body_text) == book_title:
                continue
            if len(body_text) < EMPTY_CHAPTER_CHARS:
                empty_chapters.append(hf)
            elif len(re.sub(r"\s+", "", body_text)) < SHORT_CHAPTER_CHARS:
                short_chapters.append(hf)

        result.stats["math"] = total_math
        result.stats["footnote_sections"] = total_footnote_sections
        result.stats["footnote_refs"] = total_footnote_refs
        result.stats["img_refs"] = total_img_refs
        result.stats["text_chars"] = total_text_chars
        result.stats["headings"] = total_headings
        result.stats["hard_breaks"] = total_breaks
        result.stats["tables"] = total_tables
        result.stats["code_blocks"] = total_code_blocks
        result.stats["links"] = total_links
        result.stats["links_internal"] = total_internal_links
        result.stats["empty_chapters"] = len(empty_chapters)
        result.stats["short_chapters"] = len(short_chapters)
        result.stats.update(heading_levels)

        # 声明了封面图,却没有任何文档引用它 = 阅读器打开就是空白封面页
        if cover_base and cover_base not in referenced_images:
            result.fail("cover_unreferenced",
                        f"封面图 {cover_base} 在 manifest 里,但没有任何文档引用它"
                        "(封面页会显示空白)")
        elif cover_base and cover_doc_index not in (None, 0):
            result.warn("cover_not_first",
                        f"封面所在文档排在 spine 第 {cover_doc_index + 1} 位,不是第一页")

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
            # 空章节成规模 = 内容在链路里丢了(单条空章节可能只是版权页/插页)
            if len(empty_chapters) >= max(EMPTY_CHAPTER_MIN,
                                          math.ceil(len(html_files) * EMPTY_CHAPTER_RATIO)):
                result.fail("chapters_empty_mass",
                            f"{len(empty_chapters)}/{len(html_files)} 个章节没有正文,内容疑似丢失")
        # 短章节只统计不告警(见 SHORT_CHAPTER_CHARS 注释):版权页/前言天然就短
        # ── 孤立图片:manifest 里有、但没有任何 XHTML 引用(封面除外) ──
        orphans = [h for h in img_items.values()
                   if h.split("/")[-1] not in referenced_images
                   and h.split("/")[-1] != cover_base]
        result.stats["orphan_images"] = len(orphans)
        # 封面是 pandoc 注入的,源 Markdown 里当然没有 —— 内容对照要比对的是正文图片
        result.stats["images_no_cover"] = len(img_items) - (1 if cover_base else 0)
        if orphans:
            result.warn("image_orphan",
                        f"{len(orphans)} 张图片在 manifest 里但正文从未引用: {orphans[:3]}")

        # ---- 5. TOC ----
        nav_path = None
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            if "nav" in (item.get("properties") or "").split():
                nav_path = str((opf_dir / item.get("href", "")).as_posix())
        toc_links: list[str] = []
        if nav_path and nav_path in names:
            nav_raw = zf.read(nav_path)
            nav = nav_raw.decode("utf-8", errors="replace")
            try:
                ET.fromstring(nav_raw)
            except ET.ParseError as e:
                result.fail("nav_invalid", f"nav 文档不是良构 XML(目录可能显示不出来): {e}")
            for href in re.findall(r'<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"', nav):
                linked_css.add(href.split("/")[-1])
            # 只认 toc 那一段:landmarks 里的 Cover / Title Page / Table of Contents
            # 不是目录条目(混进来会虚增目录数,也看不出真正的目录是否完整)
            seg = re.search(r'<nav[^>]*epub:type="toc"[^>]*>(.*?)</nav>', nav, re.S)
            if seg is None:
                result.warn("nav_not_toc", 'nav 文档里没有 epub:type="toc" 的导航(目录可能为空)')
            else:
                toc_links = re.findall(r'<a[^>]*href="([^"]+)"', seg.group(1))
            result.stats["toc_links"] = len(toc_links)
            # 目录链接指向包内不存在的文件 = 读者点进去是空白页(书库里的目录同样坏)
            broken_toc = [h for h in toc_links
                          if not h.startswith(("http://", "https://", "mailto:"))
                          and h.partition("#")[0]
                          and not in_zip(h.partition("#")[0], opf_dir_parts)]
            if broken_toc:
                result.fail("toc_link_broken",
                            f"目录里有 {len(broken_toc)} 条链接指向包内不存在的文件: "
                            f"{broken_toc[:5]}")
        else:
            result.warn("nav_missing", f"未找到 nav 文档 ({nav_path})")
        if not toc_links and total_headings >= TOC_EMPTY_MIN_HEADINGS:
            result.warn("toc_empty",
                        f"正文有 {total_headings} 个标题,但目录里没有任何条目")

        # ---- 5b. toc.ncx:EPUB2 / 旧版 Kindle 的目录来源,坏了同样点不动 ----
        ncx_href = next((h for h in manifest.values() if h.lower().endswith(".ncx")), None)
        if ncx_href and in_zip(ncx_href, opf_dir_parts):
            ncx_raw = zf.read(str((opf_dir / ncx_href).as_posix()))
            try:
                ncx = ET.fromstring(ncx_raw)
            except ET.ParseError as e:
                result.fail("ncx_invalid", f"toc.ncx 不是良构 XML(旧阅读器目录会失效): {e}")
            else:
                srcs = [c.get("src", "") for c in ncx.iter(f"{{{NS['ncx']}}}content")]
                result.stats["ncx_points"] = len(srcs)
                broken_ncx = [s for s in srcs
                              if s.partition("#")[0]
                              and not in_zip(s.partition("#")[0], opf_dir_parts)]
                if broken_ncx:
                    result.fail("ncx_link_broken",
                                f"toc.ncx 里有 {len(broken_ncx)} 条链接指向包内不存在的文件: "
                                f"{broken_ncx[:5]}")

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
        else:
            css_names = {h.split("/")[-1] for h in css_items}
            # 引用了不存在的样式文件 = 排版也失效(比「没样式」更难发现)
            dangling = sorted(linked_css - css_names)
            if dangling:
                result.fail("css_link_broken",
                            f"XHTML 引用了 manifest 中不存在的样式表: {dangling}")
            # 样式表在包里,却没有任何文档链接它 → 整套排版实际没有生效
            elif not (linked_css & css_names):
                result.fail("css_unlinked",
                            f"CSS 已嵌入包内({sorted(css_names)}),但没有任何文档引用它"
                            "(排版不会生效)")

    return result
