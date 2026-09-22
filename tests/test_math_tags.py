"""公式编号 ``\\tag{n}`` 的 MathML 兼容回归。

被钉住的缺陷:**MathML 没有 `\\tag` 这个概念** —— pandoc 只把它留在
``<annotation encoding="application/x-tex">`` 里,MathML 正文一个字符都不出现,
于是阅读器里**公式编号整个消失**。实测:云端 OCR 路径的
``$$\\int x \\tag{1}$$`` 转出来只有公式,编号连同 ``\\tag`` 一起没了
(本地路径则是编号被矢量簇切成独立小图 —— 那是上游 PyMuPDF4LLM 的行为,另记)。

修复:转成 ``\\qquad{(n)}``(仍属于同一个 MathML 块,编号可见),``\\tag*{n}`` 保持无括号。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from epub.pandoc import build_epub, normalize_math_tags


# ---------------------------------------------------------------- 归一化

def test_tag_becomes_visible_number() -> None:
    assert normalize_math_tags(r"$$\int x \tag{1}$$") == r"$$\int x \qquad{(1)}$$"


def test_multiple_tags_on_one_line() -> None:
    """MinerU 会把多行公式写在同一行的多个 $$…$$ 里(实测形态)。"""
    src = r"$$\sum k \tag{1}$$$$\lim x \tag{2}$$"
    assert normalize_math_tags(src) == r"$$\sum k \qquad{(1)}$$$$\lim x \qquad{(2)}$$"


def test_multiline_display_math() -> None:
    src = "$$\n(a - b)^2\n\\tag{1}\n$$\n"
    assert normalize_math_tags(src) == "$$\n(a - b)^2\n\\qquad{(1)}\n$$\n"


def test_tag_star_keeps_no_parentheses() -> None:
    assert normalize_math_tags(r"$a \tag*{A}$") == r"$a \qquad{A}$"


def test_inline_math_also_normalized() -> None:
    assert normalize_math_tags(r"前 $a = b \tag{3}$ 后") == r"前 $a = b \qquad{(3)}$ 后"


def test_code_fence_is_untouched() -> None:
    """围栏代码块里的 $ 与 \\tag 原样保留(书里讲 LaTeX 的段落不能被改)。"""
    src = "```\n$$ x \\tag{1} $$\n```\n"
    assert normalize_math_tags(src) == src


def test_prose_without_math_is_untouched() -> None:
    src = "正文里的 \\tag{1} 没有 $ 包裹,不该动\n"
    assert normalize_math_tags(src) == src


# ---------------------------------------------------------------- 端到端

def _mathml(epub: Path) -> str:
    with zipfile.ZipFile(epub) as z:
        for name in z.namelist():
            if name.endswith(".xhtml"):
                text = z.read(name).decode("utf-8")
                if "<math" in text:
                    return text
    raise AssertionError("产物里没有 MathML")


def _visible(mathml: str) -> str:
    """只看渲染出来的部分:annotation 里那份 LaTeX 源不算「可见」。"""
    return re.sub(r"<annotation.*?</annotation>", "", mathml, flags=re.S)


def test_epub_shows_equation_number_in_visible_mathml(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text(
        "# 公式书\n\n$$\n(a - b)^2 = a^2 - 2ab + b^2 \\tag{1}\n$$\n", encoding="utf-8"
    )

    epub = build_epub(md, work, tmp_path / "out", title="公式书")

    visible = _visible(_mathml(epub))
    assert "\\tag" not in visible, "MathML 正文里不该再出现 \\tag"
    assert re.search(r"<mn>1</mn>", visible), "编号 1 必须出现在可见正文里"
    assert re.search(r"<mo[^>]*>\s*\(\s*</mo>", visible), "编号应带括号显示"


def test_source_markdown_is_not_modified(tmp_path: Path) -> None:
    """book.md 是内容对照的源,不能被就地改写——改的是给 pandoc 的副本。"""
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    original = "# 公式书\n\n$$ x \\tag{1} $$\n"
    md.write_text(original, encoding="utf-8")

    build_epub(md, work, tmp_path / "out", title="公式书")

    assert md.read_text(encoding="utf-8") == original
    copy = work / "book.pandoc.md"
    assert copy.exists() and "\\tag" not in copy.read_text(encoding="utf-8")


def test_no_tag_means_no_copy(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 无编号公式\n\n$$ x = 1 $$\n", encoding="utf-8")

    build_epub(md, work, tmp_path / "out", title="无编号公式")

    assert not (work / "book.pandoc.md").exists()
