"""产物侧补书名时顺手清掉的空目录项(``add_title``)。

背景:pandoc 只要拿到 ``--metadata title`` 就会在正文首个文件顶上插一个
``<h1 class="unnumbered">书名</h1>``;同名章节因此与它撞 section id,EPUB 导航里
两条目录项指向同一处(章节条目实际丢失)。修法是「不出标题页时不把书名交给 pandoc、
由产物侧补 dc:title」—— 但那样正文章首会留下一个**空**的 unnumbered 段落,
导航里对应一条空目录项。这里钉住收尾:书名要补上、空目录项要清掉、真条目不能动。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from pdf2epub.epub.pandoc import add_title
OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:language>zh-CN</dc:language>
  </metadata>
  <guide><reference type="toc" title="UNTITLED" href="nav.xhtml" /></guide>
</package>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>UNTITLED</title></head>
<body><nav epub:type="toc"><h1 id="toc-title">UNTITLED</h1><ol>
<li id="toc-li-1"><a href="text/ch001.xhtml#section" /></li>
<li id="toc-li-2"><a href="text/ch002.xhtml#译序">译序</a></li>
</ol></nav></body></html>
"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="navPoint-1"><navLabel><text></text></navLabel><content src="text/ch001.xhtml#section" /></navPoint>
<navPoint id="navPoint-2"><navLabel><text>译序</text></navLabel><content src="text/ch002.xhtml#译序" /></navPoint>
</navMap><docTitle><text>UNTITLED</text></docTitle></ncx>
"""

CH1 = ('<html><head><title>UNTITLED</title></head><body>'
       '<h1 class="unnumbered"></h1><p>正文</p></body></html>')


def _make_epub(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("EPUB/content.opf", OPF)
        z.writestr("EPUB/nav.xhtml", NAV)
        z.writestr("EPUB/toc.ncx", NCX)
        z.writestr("EPUB/text/ch001.xhtml", CH1)
    return path


def test_title_added_and_empty_nav_entries_removed(tmp_path: Path) -> None:
    epub = _make_epub(tmp_path / "t.epub")
    assert add_title(epub, "烧纸") is True

    z = zipfile.ZipFile(epub)
    assert "<dc:title>烧纸</dc:title>" in z.read("EPUB/content.opf").decode("utf-8")
    nav = z.read("EPUB/nav.xhtml").decode("utf-8")
    assert "toc-li-1" not in nav and "译序" in nav          # 空条目没了,真条目还在
    ncx = z.read("EPUB/toc.ncx").decode("utf-8")
    assert "navPoint-1" not in ncx and "译序" in ncx
    ch = z.read("EPUB/text/ch001.xhtml").decode("utf-8")
    assert '<h1 class="unnumbered"></h1>' not in ch
    assert "<title>烧纸</title>" in ch                    # 章节文件 head 里是书名

    # 目录页自己的标题:pandoc 没拿到书名时会写 UNTITLED(阅读器的目录页就长这样)
    # 目录页自己的标题是「目录」,不是书名(pandoc 有书名时会写成书名,没书名时写 UNTITLED)
    assert "<title>目录</title>" in nav and "UNTITLED" not in nav
    assert '<h1 id="toc-title">目录</h1>' in nav
    assert "UNTITLED" not in ncx and "<text>目录</text>" in ncx
    # OPF2 的 guide 里还有一份目录标题(老阅读器用它)
    opf = z.read("EPUB/content.opf").decode("utf-8")
    assert "UNTITLED" not in opf and 'type="toc" title="目录"' in opf
    assert "<dc:title>烧纸</dc:title>" in opf


def test_mimetype_stays_first_and_stored(tmp_path: Path) -> None:
    """重写 zip 不得破坏 mimetype 的「第一且不压缩」—— 否则严格阅读器直接报废。"""
    epub = _make_epub(tmp_path / "t.epub")
    add_title(epub, "烧纸")
    with zipfile.ZipFile(epub) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED


def test_idempotent(tmp_path: Path) -> None:
    """再调一次不该插第二个 dc:title(重复调用是同一条流水线上的常态)。"""
    epub = _make_epub(tmp_path / "t.epub")
    add_title(epub, "烧纸")
    add_title(epub, "烧纸")
    with zipfile.ZipFile(epub) as z:
        assert z.read("EPUB/content.opf").decode("utf-8").count("<dc:title>") == 1


# ---------------------------------------------------------------- 目录页排版
# pandoc 的 epub3 writer 把目录写成嵌套的 <ol class="toc">,阅读器按有序列表渲染
# 成 1./1.1/1.1.1 数字序号。章节层级靠缩进就够,数字既多余又和书里自己排的
# 小节号(``1 麦园``)打架 —— 产物侧换成 <ul>,只留圆点标识。

TOC_NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>目录</title></head>
<body><nav epub:type="toc"><h1 id="toc-title">目录</h1><ol class="toc">
<li id="toc-li-1"><a href="text/ch001.xhtml#a">甲篇</a>
<ol class="toc"><li id="toc-li-2"><a href="text/ch001.xhtml#b">甲篇小节</a></li></ol></li>
</ol></nav></body></html>
"""


def _make_toc_epub(path: Path, nav: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("EPUB/content.opf", OPF)
        z.writestr("EPUB/nav.xhtml", nav)
        z.writestr("EPUB/toc.ncx", NCX)
        z.writestr("EPUB/text/ch001.xhtml", CH1)
    return path


def test_unnumber_toc_hides_numbers_without_breaking_the_spec(tmp_path: Path) -> None:
    """用 CSS 去编号,``<ol>`` 必须原样保留。

    被钉住的缺陷:旧实现把目录列表换成 ``<ul>`` —— 数字没了,但 EPUB 3 规定
    ``nav[epub:type="toc"]`` 里必须是有序列表,EPUBCheck 对**每一本产物**报两条
    `RSC-005`(nav incomplete / ul not allowed here),整批书不合规。
    """
    from pdf2epub.epub.pandoc import TOC_NUMBER_CSS, unnumber_toc

    epub = _make_toc_epub(tmp_path / "t.epub", TOC_NAV)
    assert unnumber_toc(epub) == 2
    with zipfile.ZipFile(epub) as z:
        nav = z.read("EPUB/nav.xhtml").decode("utf-8")
        assert nav.count('<ol class="toc">') == 2 and nav.count("</ol>") == 2
        assert "<ul" not in nav, "目录列表不能换成 ul(EPUB3 规范要求 ol)"
        assert TOC_NUMBER_CSS in nav, "去编号的 CSS 没注入"
        assert "甲篇" in nav and "甲篇小节" in nav          # 条目文字不丢
        assert z.read("EPUB/toc.ncx") == NCX.encode("utf-8")  # ncx 不显示数字,不用动
        assert z.infolist()[0].filename == "mimetype"
        assert z.infolist()[0].compress_type == zipfile.ZIP_STORED
    assert unnumber_toc(epub) == 0                         # 幂等


def test_unnumber_toc_leaves_mixed_lists_alone(tmp_path: Path) -> None:
    """nav 里混着别的 ``<ol>`` 时结构一个字节不动:CSS 只作用于 ``ol.toc``。"""
    from pdf2epub.epub.pandoc import TOC_NUMBER_CSS, unnumber_toc

    mixed = TOC_NAV.replace("</ol></nav>", '<ol><li>别的有序列表</li></ol></ol></nav>')
    epub = _make_toc_epub(tmp_path / "t.epub", mixed)
    assert unnumber_toc(epub) == 2
    with zipfile.ZipFile(epub) as z:
        nav = z.read("EPUB/nav.xhtml").decode("utf-8")
        assert '<ol class="toc">' in nav
        assert "<ol><li>别的有序列表</li></ol>" in nav
        assert TOC_NUMBER_CSS in nav


def test_book_css_styles_toc_list() -> None:
    css = (Path(__file__).resolve().parents[1] / "config" / "book.css").read_text(
        encoding="utf-8")
    assert "ol.toc" in css and "list-style-type: none" in css
    assert "ul.toc" not in css, "目录还是 <ol>,CSS 不该再按 ul 写"
