"""Pandoc EPUB 生成封装(idea.md §10/§11)。

统一命令:
    pandoc book.md -o out.epub --toc --toc-depth=3 --css=config/book.css
           --resource-path=<work> --mathml --metadata title/author/lang
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from paths import book_css

DEFAULT_CSS = book_css()

#: 数学片段:$$…$$ / $…$ / \[…\] / \(…\)(本地路径的公式是图片,只有云端 LaTeX 会命中)
MATH_SPAN_RE = re.compile(r"\$\$.+?\$\$|\$[^$\n]+\$|\\\[.+?\\\]|\\\(.+?\\\)", re.S)
#: LaTeX 公式编号:\tag{1} / \tag*{1}
MATH_TAG_RE = re.compile(r"\\tag(\*?)\s*\{([^{}]*)\}")
#: 围栏代码块(整段原样保留,里面的 $ 与 \tag 不动)。
#: 必须带捕获组:否则 ``re.split`` 会把分隔符(整个代码块)**丢掉**。
FENCE_RE = re.compile(r"(^```.*?^```)", re.S | re.M)


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
        RuntimeError: Pandoc 执行失败
    """
    book_md = Path(book_md)
    work_dir = Path(work_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    epub = output_dir / f"{out_name or book_md.parent.name}.epub"
    title = title or infer_title(book_md)

    # MathML 不认识 \tag:公式编号会静默消失 → 先归一化成可见的 \qquad(n)。
    # 不就地改 book.md(它是内容对照的源),只在需要时写一份给 pandoc 用的副本。
    source_md = book_md
    md_text = book_md.read_text(encoding="utf-8", errors="replace")
    if "\\tag" in md_text:
        normalized = normalize_math_tags(md_text)
        if normalized != md_text:
            source_md = book_md.parent / f"{book_md.stem}.pandoc.md"
            source_md.write_text(normalized, encoding="utf-8")

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
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"Pandoc 失败(exit={proc.returncode}): {proc.stderr[:500]}")
    return epub
