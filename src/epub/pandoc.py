"""Pandoc EPUB 生成封装(idea.md §10/§11)。

统一命令:
    pandoc book.md -o out.epub --toc --toc-depth=3 --css=config/book.css
           --resource-path=<work> --mathml --metadata title/author/lang

生成前会把 Markdown **副本**里两类「Markdown 合法、EPUB 非法」的写法归一化
(`\\tag{n}` 公式编号、原始 HTML 空元素),不就地改 `book.md`(它是内容对照的源):

  - `\\tag{n}`:MathML 没有这个概念,编号会在阅读器里整个消失。
  - `<br>` / `<img>` 等空元素:HTML 允许省略斜杠,XHTML(EPUB 的正文格式)不允许 ——
    pandoc 原样透传原始 HTML,一个 `<br>` 就足以让整章 XHTML 不再是良构 XML,
    严格阅读器会拒绝渲染(云端 OCR 的图片文字块就带 `<br>`)。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from paths import book_css

DEFAULT_CSS = book_css()

#: pandoc 可执行文件名(Windows 上同样是 `pandoc`,PATHEXT 由 shutil.which 处理)
PANDOC = "pandoc"

#: 环境缺失时的提示(装完 pandoc 需要重开终端/桌面端才会进 PATH)
MISSING_PANDOC_HINT = (
    "未找到 pandoc(生成 EPUB 的必需组件):请安装 pandoc 并确保它在 PATH 中"
    "(Windows 上装完需要重开终端或桌面端),然后重跑"
)


class PandocMissingError(RuntimeError):
    """pandoc 不存在(环境缺失)。

    **确定性失败,不该重试**:重试只会把同一堵墙再撞一遍,而每次重试都要重跑
    整条链路(提取 + 云端 OCR),白烧云端额度。
    """


def pandoc_available() -> bool:
    """pandoc 是否在 PATH 里(只探测,不执行)。"""
    return shutil.which(PANDOC) is not None


def require_pandoc() -> None:
    """pandoc 缺失时抛 PandocMissingError(调用方可据此提前失败,不进入昂贵阶段)。"""
    if not pandoc_available():
        raise PandocMissingError(MISSING_PANDOC_HINT)

#: 数学片段:$$…$$ / $…$ / \[…\] / \(…\)(本地路径的公式是图片,只有云端 LaTeX 会命中)
MATH_SPAN_RE = re.compile(r"\$\$.+?\$\$|\$[^$\n]+\$|\\\[.+?\\\]|\\\(.+?\\\)", re.S)
#: LaTeX 公式编号:\tag{1} / \tag*{1}
MATH_TAG_RE = re.compile(r"\\tag(\*?)\s*\{([^{}]*)\}")
#: 围栏代码块(整段原样保留,里面的 $ 与 \tag 不动)。
#: 必须带捕获组:否则 ``re.split`` 会把分隔符(整个代码块)**丢掉**。
FENCE_RE = re.compile(r"(^```.*?^```)", re.S | re.M)

#: XHTML 空元素:HTML 允许写 `<br>`,XML 必须自闭合,否则整份文档不再是良构 XML。
VOID_TAGS = ("area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr")
VOID_TAG_RE = re.compile(
    r"<(?P<name>" + "|".join(VOID_TAGS) + r")\b(?P<attrs>[^<>]*?)(?:/)?\s*>",
    re.I,
)


def normalize_math_tags(text: str) -> str:
    r"""把 ``\tag{n}`` / ``\tag*{n}`` 换成**可见**的编号。

    MathML 没有 ``\tag`` 这个概念:pandoc 只会把它留在 ``<annotation
    encoding="application/x-tex">`` 里,公式正文里一个字符都不出现 —— 阅读器里
    **公式编号直接消失**(实测:云端路径的 ``$$\int x \tag{1}$$`` 转出来只有公式,
    连同编号一起丢了)。换成 ``\qquad{(1)}`` 后编号跟着公式一起渲染,且仍属于同一
    个 MathML 块。``\tag*{n}`` 本义是没有括号,这里同样不加。

    只改数学片段;围栏代码块里的同名字样原样保留。
    """
    def fix_span(match: re.Match) -> str:
        span = match.group(0)
        if "\\tag" not in span:
            return span

        def fix_tag(tag: re.Match) -> str:
            star, label = tag.group(1), tag.group(2).strip()
            return f"\\qquad{{{label}}}" if star else f"\\qquad{{({label})}}"

        return MATH_TAG_RE.sub(fix_tag, span)

    parts = FENCE_RE.split(text)
    # ``split`` 的分隔符本身会留在结果里(奇数下标即围栏块),它们不参与替换
    for i in range(0, len(parts), 2):
        parts[i] = MATH_SPAN_RE.sub(fix_span, parts[i])
    return "".join(parts)


def normalize_xhtml_voids(text: str) -> str:
    r"""把原始 HTML 的空元素写成自闭合形式(`<br>` → `<br />`)。

    XHTML 是 XML:`<br>` 是**未闭合标签**,一个就够让整章不再是良构 XML ——
    Readium / KOReader / epubcheck 会直接拒绝或错乱渲染,而 pandoc 对 Markdown 里的
    原始 HTML 是**原样透传**的(云端 OCR 的图片文字块常带 `<br>`;实测一本 300KB 的
    章节就因为这一个标签整份失效)。归一化保留标签语义(换行仍然换行),只是把它
    写对。

    只改正文里的原始 HTML;围栏代码块里的同名字样是示例文本,原样保留。
    """
    def fix(match: re.Match) -> str:
        tag = match.group("name")
        attrs = match.group("attrs").strip()
        return f"<{tag} {attrs} />" if attrs else f"<{tag} />"

    parts = FENCE_RE.split(text)
    for i in range(0, len(parts), 2):        # 奇数下标是围栏代码块
        parts[i] = VOID_TAG_RE.sub(fix, parts[i])
    return "".join(parts)


def prepare_pandoc_markdown(text: str) -> str:
    """把 Markdown 归一化成「Pandoc 能写出合法 EPUB3」的形式(见模块 docstring)。"""
    return normalize_xhtml_voids(normalize_math_tags(text))


def infer_title(book_md: Path, fallback: str | None = None) -> str:
    """从 Markdown 标题推断书名:跳过过短/无意义的候选(如版权页'说明')。"""
    try:
        text = book_md.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^#\s+(.+)$", text, re.MULTILINE):
            candidate = m.group(1).strip()
            condensed = re.sub(r"\s+", "", candidate)
            if len(condensed) >= 3:
                return candidate
    except OSError:
        pass
    return fallback or book_md.parent.name


#: 脚注块(pandoc 生成:`<aside epub:type="footnote" … id="fn1">…</aside>`)
FOOTNOTE_ASIDE_RE = re.compile(
    r'(<aside\b[^>]*epub:type="footnote"[^>]*>)(.*?)</aside>', re.S)
#: 脚注块上的 id(回链要指回 `fnref<后缀>`)
FOOTNOTE_ID_RE = re.compile(r'\bid="([^"]+)"')


def add_footnote_backlinks(epub_path: str | Path) -> int:
    """给脚注块补「回到正文引用处」的回链,返回补了几条。

    **为什么需要这一步**:pandoc 3.x 的 epub3 writer 只写正文侧的引用
    (`<a href="#fn1" class="footnote-ref" id="fnref1" epub:type="noteref">`),
    **不写**脚注侧的 `doc-backlink`(用最小样本实测过,与本项目代码无关)。
    没有回链时「返回正文」只能靠阅读器自己的返回键,规范推荐的回链形式是脚注里带
    `<a href="#fnref1" role="doc-backlink">`。

    **只在产物里确实有脚注时才动文件**:没有脚注的书连 zip 都不重写(零风险)。
    重写时 `mimetype` 必须仍是第一个条目且不压缩(EPUB 规范要求,否则严格阅读器报废)。
    """
    path = Path(epub_path)
    try:
        with zipfile.ZipFile(path) as zf:
            items = [(i, zf.read(i.filename)) for i in zf.infolist()]
    except (zipfile.BadZipFile, OSError):
        return 0

    changed = 0
    new_items: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, data in items:
        name = info.filename
        if not name.endswith((".xhtml", ".html", ".htm")) or b"footnote" not in data:
            new_items.append((info, data))
            continue
        text = data.decode("utf-8", errors="replace")

        def fix(match: re.Match) -> str:
            nonlocal changed
            tag, body = match.group(1), match.group(2)
            found = FOOTNOTE_ID_RE.search(tag)
            if found is None:                                # 没有 id 就没法指回来
                return match.group(0)
            anchor = found.group(1)
            ref_id = (f"fnref{anchor[len('fn'):]}" if anchor.startswith("fn")
                      else f"fnref-{anchor}")
            if f'href="#{ref_id}"' in body:                  # 已经有回链:不重复补
                return match.group(0)
            back = (f' <a href="#{ref_id}" class="footnote-back" '
                    f'role="doc-backlink">\u21a9</a>')
            if "</p>" in body:
                head, _, tail = body.rpartition("</p>")
                body = f"{head}{back}</p>{tail}"
            else:
                body = f"{body}{back}"
            changed += 1
            return f"{tag}{body}</aside>"

        new_items.append((info, FOOTNOTE_ASIDE_RE.sub(fix, text).encode("utf-8")))

    if not changed:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in new_items:
            if info.filename == "mimetype":                  # 必须第一且不压缩
                out.writestr(zipfile.ZipInfo("mimetype"), data, zipfile.ZIP_STORED)
            else:
                out.writestr(info.filename, data, zipfile.ZIP_DEFLATED)
    os.replace(tmp, path)
    return changed


def build_epub(
    book_md: str | Path,
    work_dir: str | Path,
    output_dir: str | Path,
    title: str | None = None,
    author: str | None = None,
    css: str | Path = DEFAULT_CSS,
    out_name: str | None = None,
    lang: str | None = None,
    identifier: str | None = None,
    date: str | None = None,
    cover_image: str | Path | None = None,
) -> Path:
    """用 Pandoc 将 work/book.md 转为 EPUB,返回 epub 路径。

    out_name: 输出文件名(不含 .epub)。默认取 work 目录名(被 sanitize 过),
    调用方应传原始「标题 - 作者」名,避免空格被写成下划线。
    lang: dc:language(默认 zh-CN;由调用方做检测/覆盖)。
    identifier: dc:identifier(稳定 UUID,见 batch.stable_identifier)。
    date: dc:date(ISO 日期)。
    cover_image: 封面 JPEG(pandoc --epub-cover-image)。

    Raises:
        PandocMissingError: pandoc 不在 PATH(环境缺失,不该重试)
        RuntimeError: Pandoc 执行失败
    """
    book_md = Path(book_md)
    work_dir = Path(work_dir)
    output_dir = Path(output_dir)
    # 先确认 pandoc 存在,再产生任何副作用(建目录、写 pandoc 用的副本):
    # 环境缺失必须立刻暴露,而不是等到链路末尾
    require_pandoc()
    output_dir.mkdir(parents=True, exist_ok=True)

    epub = output_dir / f"{out_name or book_md.parent.name}.epub"
    title = title or infer_title(book_md)

    # MathML 不认识 \tag:公式编号会静默消失 → 先归一化成可见的 \qquad(n)。
    # XHTML 不接受原始 HTML 的空元素 → `<br>` 顺手写成 `<br />`。
    # 不就地改 book.md(它是内容对照的源),只在确有改动时写一份给 pandoc 用的副本。
    source_md = book_md
    md_text = book_md.read_text(encoding="utf-8", errors="replace")
    prepared = prepare_pandoc_markdown(md_text)
    if prepared != md_text:
        source_md = book_md.parent / f"{book_md.stem}.pandoc.md"
        source_md.write_text(prepared, encoding="utf-8")

    cmd = [
        "pandoc", str(source_md), "-o", str(epub),
        "--toc", "--toc-depth=3",
        "--css", str(css),
        "--resource-path", str(work_dir),
        "--mathml",
        "--metadata", f"title={title}",
        "--metadata", f"lang={lang or 'zh-CN'}",
    ]
    if author:
        cmd += ["--metadata", f"author={author}"]
    if identifier:
        cmd += ["--metadata", f"identifier={identifier}"]
    if date:
        cmd += ["--metadata", f"date={date}"]
    if cover_image:
        cmd += ["--epub-cover-image", str(cover_image)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    except FileNotFoundError as e:
        # 极少数情况下 which() 能解析、真正执行时却找不到(Windows: [WinError 2])。
        # 这同样是环境缺失,不是「临时故障」—— 归成可重试错误会让整条链路白跑。
        raise PandocMissingError(f"{MISSING_PANDOC_HINT}(执行 pandoc 失败: {e})") from e
    if proc.returncode != 0:
        raise RuntimeError(f"Pandoc 失败(exit={proc.returncode}): {proc.stderr[:500]}")
    # 退出码 0 不等于「写出了产物」:产物缺失或 0 字节都是硬失败,不能把空文件
    # 当成功结果交给上层(is_done 只认容器完整性,空文件会在这里就暴露)。
    if not epub.exists() or epub.stat().st_size == 0:
        raise RuntimeError(f"Pandoc 返回成功但没有写出 EPUB(产物缺失或为空): {epub}")
    # 脚注回链:pandoc 的 epub3 writer 不写 doc-backlink,产物侧补上(没有脚注就不动文件)
    add_footnote_backlinks(epub)
    return epub
