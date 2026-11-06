"""书名 / 作者 / 出版社:按可信度分级取,每一级都记下自己来自哪里。

以前只认文件名里的「标题 - 作者」。文件名通常是用户自己起的,最准 —— 但扫描仪和
下载站给的名字是 `扫描0001`、`IMG_2043`、`新建 DOC 文档`,而书本身在 PDF 元数据、
扉页字段、正文第一个大标题上都写着书名。反过来,文件名写着「巴勒斯坦问题 - 张三」
时最可靠,而同一份 PDF 的元数据里躺着「Microsoft Word - 巴勒斯坦问题.doc」这种
谁也不想要的东西。

所以分级回落,并把每个字段的来源记进 ``BookMeta.sources`` —— 书名出问题时不用猜
是哪一级给的:

  1. 文件名
  2. PDF 文档元数据(Title / Author)
  3. 卷首字段(清理**前**的第 1 页:``作者:`` ``出版社:`` ``译者:``)
  4. 正文第一个 H1(只补书名)

三条硬规矩:

- **作者位只放作者本人**。扉页上的「译者」「编者」不进 author(``front_fields``
  里,同一行出现下一个标签就截断);译者另走 ``BookMeta.translator``。
- **垃圾值当没有**。``Administrator``、``Microsoft Word - x.doc`` 一旦进 OPF,
  阅读器书架上就是一片这种书名 —— 不如留空,让文件名兜底。
- 卷首字段只从**行首**取、只取短行。前言里「本书作者认为……」这种句子不该被
  当成元数据;判据是「以标签+冒号开头」「值不含句末标点」「值不超过 40 字」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: 只扫文首这么多行。卷首字段(书名/作者/出版社)不会出现在第一章之后。
FRONT_SCAN_LINES = 40
#: 字段值上限。真封面上的书名/作者/社名都很短,超过就是句子。
FIELD_VALUE_MAX = 40
#: 「第一个 H1」当书名候选时的上限
HEADING_TITLE_MAX = 80

#: 下载站/扫描仪起的名字。命中即认为「文件名没提供书名」,继续往下找。
_SCAN_NAME_RE = re.compile(
    r"^(?:扫描|掃描|扫描件|扫描全|scan(?:ned)?|img|image|pic|dsc|dscn|photo|"
    r"doc|document|file|new|新建|未命名|无标题|untitled|unnamed|ebook|电子书|"
    r"output|out|book)[\s_\-]*(?:no\.?)?\s*\d*$",
    re.I,
)
#: PDF 元数据里那种「其实是文件名」的标题
_OFFICE_TITLE_RE = re.compile(
    r"microsoft\s+(?:word|powerpoint|excel)|untitled|无标题|未命名", re.I)
#: Windows 新建文件的默认名:「新建 Microsoft Word 文档」「新建文本文档」「新建 DOC 文档」。
#: 中间只许出现 ASCII 字母 —— 否则「新建 我的书 文档」这种真书名也会被算成默认名。
_BLANK_DOC_RE = re.compile(
    r"^(?:新建|new)\s*(?:[A-Za-z]+\s*)*(?:文档|文本文档|document|doc)$", re.I)
_DOC_SUFFIX_RE = re.compile(r"\.(?:docx?|pdf|indd|pages|rtf|txt|md|wps|odt|html?)$", re.I)

#: 元数据里的作者垃圾值(Windows 用户名、软件名、占位词)
_JUNK_AUTHORS = frozenset({
    "administrator", "admin", "user", "owner", "author", "作者", "unknown",
    "none", "null", "microsoft office user", "wps", "lenovo", "hp", "pc",
    "windows user", "微软用户", "微软中国",
})

#: 作者名后的身份后缀(「[美] 庞德 著」)。只去尾巴,不动中间的「著」字。
_AUTHOR_TAIL_RE = re.compile(r"[\s　]*(?:著|编著|主编|合著|编|等|撰|译)$")

#: 卷首一行的字段标签。**必须在行首**(前面可能有项目符号/井号,调用方先剥掉);
#: 一行可以有多个字段(「作者:柏拉图 译者:郭斌和」),所以按标签切段而不是整行匹配。
_LABEL_RE = re.compile(
    r"(?:书\s*名|题\s*名|作\s*者|著\s*者|译\s*者|翻\s*译|编\s*者|主\s*编|"
    r"出\s*版\s*社|出版者|出版发行|出版日期|出版时间|定\s*价|isbn|页\s*数|字\s*数|开\s*本)"
    r"\s*[:：]",
    re.I,
)
#: 标签 → 字段名。**不在这张表里的标签**(定价/ISBN/开本…)只用来切段,不取值 ——
#: 不取,但必须切:否则「作者:柏拉图 ISBN:9787…」会把 ISBN 一起算进作者。
_LABEL_KEYS = {
    "书名": "title", "题名": "title",
    "作者": "author", "著者": "author",
    "译者": "translator", "翻译": "translator",
    "出版社": "publisher", "出版者": "publisher", "出版发行": "publisher",
}
_FIELD_NAMES = ("title", "author", "translator", "publisher")
#: 字段行不以句末标点收尾;以此收尾的是正文句子
_SENTENCE_END = tuple("。!?…；;")

_LINE_PREFIX_RE = re.compile(r"^[\s>#*\-–—•·|]+")
_WS_RE = re.compile(r"[\s\u3000]+")
_ATX_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
#: 这些标题是结构名,不是书名
_STRUCT_HEADINGS = frozenset({
    "前言", "序", "序言", "自序", "导言", "导论", "引言", "绪论", "目录", "目次",
    "后记", "跋", "附录", "参考文献", "译后记", "译序", "编者的话", "出版说明",
    "版权页", "扉页", "内容简介", "作者简介",
})


@dataclass(frozen=True)
class BookMeta:
    """一本书的元数据 + 每个字段的来源(``filename`` / ``pdf`` / ``front_matter`` / ``heading``)。"""

    title: str
    author: str | None = None
    publisher: str | None = None
    translator: str | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "title": self.title, "author": self.author,
            "publisher": self.publisher, "translator": self.translator,
            **{f"source_{k}": v for k, v in self.sources.items()},
        }


def _clean(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def _junk_name(title: str) -> bool:
    """这个名字能不能当书名 —— 「扫描0001」「0001」「IMG_2043」不能。"""
    text = _clean(title)
    if len(text) < 2:
        return True
    if not re.search(r"[^\W\d_]", text):        # 全是数字/符号/下划线:没有字母
        return True
    return bool(_SCAN_NAME_RE.match(text) or _BLANK_DOC_RE.match(text))


def _junk_pdf_title(title: str) -> bool:
    """PDF 元数据里的 Title 往往是「Microsoft Word - 报告.doc」,等于没说。"""
    text = _clean(title)
    if _junk_name(text):
        return True
    return bool(_OFFICE_TITLE_RE.search(text) or _DOC_SUFFIX_RE.search(text))


def _junk_author(author: str) -> bool:
    text = _clean(author)
    return not text or text.lower() in _JUNK_AUTHORS or _junk_name(text)


def _clean_person(name: str) -> str:
    """去掉「著/编著/主编」这类身份后缀,只留人名。"""
    text = _AUTHOR_TAIL_RE.sub("", _clean(name)).strip(" 　,，、;；")
    return _clean(text)


def _is_struct_heading(text: str) -> bool:
    """「前言」「目录」「第一章」这类是结构名,不是书名。"""
    plain = re.sub(r"[\s\d.、,，:：]+", "", text)
    if plain in _STRUCT_HEADINGS:
        return True
    try:
        from clean.rules import is_chapter_anchor

        return is_chapter_anchor(text)
    except Exception:                       # noqa: BLE001 - 拿不到规则就当不是
        return False


# ---------------------------------------------------------------- 各来源


def pdf_document_meta(source: str | Path) -> dict[str, str]:
    """PDF 自带的 Title / Author(已滤掉垃圾值)。读不到就给空字典。"""
    try:
        import pymupdf

        with pymupdf.open(source) as doc:
            raw = dict(doc.metadata or {})
    except Exception:                       # noqa: BLE001 - 元数据读不到不是失败理由
        return {}

    meta: dict[str, str] = {}
    title = _clean(raw.get("title"))
    if title and not _junk_pdf_title(title):
        meta["title"] = title
    author = _clean(raw.get("author"))
    if not _junk_author(author):
        meta["author"] = _clean_person(author)
    return meta


def front_fields(md_text: str) -> dict[str, str]:
    """从**清理前**的正文里读卷首字段(书名/作者/译者/出版社)。

    只认行首标签 + 冒号,而且值要短、不带句末标点 —— 卷首几行是字段,往下的
    第一段就是正文了,靠这三条把两者分开。
    """
    out: dict[str, str] = {}
    if not md_text:
        return out
    for line in md_text.splitlines()[:FRONT_SCAN_LINES]:
        text = _LINE_PREFIX_RE.sub("", line).strip()
        if not text or len(text) > FIELD_VALUE_MAX * 3:
            continue
        for key, value in _line_fields(text).items():
            out.setdefault(key, value)
        if len(out) == len(_FIELD_NAMES):
            break
    return out


def _line_fields(text: str) -> dict[str, str]:
    """切一行里的字段(可能不止一个)。标签不在行首就不是字段行。"""
    marks = list(_LABEL_RE.finditer(text))
    if not marks or marks[0].start() != 0:
        return {}
    out: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        key = _LABEL_KEYS.get(_WS_RE.sub("", mark.group(0)).rstrip(":："))
        if not key:
            continue
        value = _field_value(text[mark.end():end])
        if value and key not in out:
            out[key] = value
    return out


def _field_value(raw: str) -> str:
    value = _clean(raw).strip(" 　:：,，、;；-–—")
    if not value or len(value) > FIELD_VALUE_MAX:
        return ""
    if value.endswith(_SENTENCE_END):
        return ""
    return value


def first_heading(md_text: str, *, max_level: int = 6) -> str | None:
    """正文第一个「像书名」的标题。章名与结构名(前言/目录)不算。"""
    if not md_text:
        return None
    for m in _ATX_RE.finditer(md_text):
        text = _clean(m.group(1).strip("*_` "))
        if not text or len(text) > HEADING_TITLE_MAX:
            continue
        if _junk_name(text) or _is_struct_heading(text):
            continue
        return text
    return None


# ---------------------------------------------------------------- 分级


def resolve_book_meta(
    *,
    stem_title: str,
    stem_author: str | None = None,
    pdf_meta: dict[str, str] | None = None,
    front: dict[str, str] | None = None,
    heading: str | None = None,
) -> BookMeta:
    """按 文件名 > PDF 元数据 > 卷首字段 > 首个 H1 取值。书名永不为空。"""
    pdf_meta = pdf_meta or {}
    front = front or {}
    sources: dict[str, str] = {}

    title = ""
    if stem_title and not _junk_name(stem_title):
        title, sources["title"] = _clean(stem_title), "filename"
    if not title:
        candidate = _clean(pdf_meta.get("title"))
        if candidate and not _junk_pdf_title(candidate):
            title, sources["title"] = candidate, "pdf"
    if not title:
        candidate = _clean(front.get("title"))
        if candidate and not _junk_name(candidate):
            title, sources["title"] = candidate, "front_matter"
    if not title:
        candidate = _clean(heading)
        if candidate and not _junk_name(candidate) and not _is_struct_heading(candidate):
            title, sources["title"] = candidate, "heading"
    if not title:
        # 四级都没拿到:宁可用那个「扫描0001」,也不要空标题
        # (空 dc:title 会让阅读器书架显示成文件名以外的空白卡片)
        title, sources["title"] = _clean(stem_title) or "未命名", "filename"

    author = ""
    for source_name, candidate in (
        ("filename", _clean(stem_author)),
        ("pdf", _clean(pdf_meta.get("author"))),
        ("front_matter", _clean(front.get("author"))),
    ):
        if candidate and not _junk_author(candidate):
            author, sources["author"] = _clean_person(candidate), source_name
            break

    publisher = _clean(front.get("publisher"))
    if publisher:
        sources["publisher"] = "front_matter"
    translator = _clean(front.get("translator"))
    if translator:
        sources["translator"] = "front_matter"

    return BookMeta(
        title=title,
        author=author or None,
        publisher=publisher or None,
        translator=translator or None,
        sources=sources,
    )


__all__ = [
    "BookMeta", "FIELD_VALUE_MAX", "FRONT_SCAN_LINES",
    "first_heading", "front_fields", "pdf_document_meta", "resolve_book_meta",
]
