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

from epub.pandoc import add_title

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
