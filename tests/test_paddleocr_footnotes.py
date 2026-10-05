"""PaddleOCR-VL 接入统一脚注层(离线单测)。

被测的是「云端 JSONL → 归一化块 → **统一脚注引擎**」这条适配路径:全程用假云端
(`FakePaddle`,与 `test_paddleocr_resume.py` 同一套),不发真实请求。

覆盖:单脚注 / 多脚注 / `$ ^{①} $` 与裸圈码 / 公式里的 `①` 不误伤 / 同 marker 不同页
不串 / 无法确认时安全保留(且不重复) / 结构化块随段缓存落盘 / resume 不重新 OCR /
老缓存(无结构化结果)照样 merge / Cleaner → Pandoc → EPUB 原生脚注全链路。

匹配与渲染全部发生在 `markdown.footnotes` 那一个引擎里(与 MinerU 共用);这里验的是
**适配器**:云端 JSONL → 归一化块(同形 content_list)→ 引擎,以及缓存 / resume 接线。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backends import paddleocr_backend
from backends.paddleocr_backend import (CONTENT_LIST_FILE, PaddleOCRAdapter,
                                        structured_blocks)
from conftest import make_pdf
from test_paddleocr_resume import FakePaddle

PAGE = (1190, 1684)          # 真实云端给的页面尺寸(A4 @144dpi,像素)


def block(label: str, content: str, bbox: list[int]) -> dict:
    return {"block_label": label, "block_bbox": bbox, "block_content": content}


def page_jsonl(pages: list[tuple[str, list[dict]]]) -> str:
    """按 [(markdown, parsing_res_list)] 造一份云端 JSONL —— 每行一页,与真实结构同形。"""
    lines = []
    for markdown, blocks in pages:
        lines.append(json.dumps({"result": {
            "dataInfo": {"numPages": 1, "type": "pdf",
                         "pages": [{"width": PAGE[0], "height": PAGE[1]}]},
            "layoutParsingResults": [{"markdown": {"text": markdown, "images": {}},
                                      "prunedResult": {"parsing_res_list": blocks}}],
        }}, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def body_page(text: str, notes: list[str], *, top: int = 300) -> tuple[str, list[dict]]:
    """一页:正文块 + 页底脚注块。markdown 里把脚注也带出来(真实 PaddleOCR 行为)。"""
    blocks = [block("text", text, [114, top, 1070, top + 60])]
    markdown = text
    for i, note in enumerate(notes):
        y = 1400 + i * 40
        blocks.append(block("footnote", note, [115, y, 708, y + 26]))
        markdown += "\n\n" + note
    return markdown, blocks


def _adapter(**kwargs) -> PaddleOCRAdapter:
    return PaddleOCRAdapter(token="fake-token", poll_interval=0, **kwargs)


class Cloud:
    """假云端:start(jsonl) 装上(monkeypatch 自动复原),counts() 断言有没有重复提交。"""

    def __init__(self, monkeypatch) -> None:
        self._monkeypatch = monkeypatch
        self.fake: FakePaddle | None = None

    def start(self, jsonl: str) -> FakePaddle:
        self.fake = FakePaddle(jsonl=jsonl)
        self._monkeypatch.setattr(paddleocr_backend, "requests", self.fake)
        return self.fake

    def counts(self) -> dict:
        assert self.fake is not None, "先调用 start()"
        return self.fake.counts()


@pytest.fixture()
def fake(monkeypatch) -> Cloud:
    return Cloud(monkeypatch)


def convert(tmp_path: Path, jsonl: str | None = None):
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=1)
    return _adapter().convert(pdf, tmp_path / "work")


# ---------------------------------------------------------------- 块归一化

def test_structured_blocks_normalise_to_the_shared_shape() -> None:
    """`footnote` → `page_footnote`;bbox 从像素归一化到 0-1000;空文本块丢掉。"""
    blocks = structured_blocks({"prunedResult": {"parsing_res_list": [
        block("paragraph_title", "第一章", [116, 145, 410, 185]),
        block("text", "正文一段。", [114, 302, 1070, 367]),
        block("footnote", "① 注释。", [115, 1420, 708, 1445]),
        block("image", "", [0, 0, 10, 10]),
    ]}}, 0, PAGE)

    assert [b["type"] for b in blocks] == ["text", "text", "page_footnote"]
    assert all(b["page_idx"] == 0 for b in blocks)
    note = blocks[-1]
    assert note["text"] == "① 注释。"
    # 1420 / 1684 * 1000 ≈ 843 → 页面底部(与 MinerU 的 bbox 约定一致)
    assert 800 < note["bbox"][1] < 900


# ---------------------------------------------------------------- 单 / 多脚注

def test_single_footnote_becomes_a_pandoc_footnote(tmp_path, fake) -> None:
    """正文 `$ ^{①} $` + 页底 `①` → `正文[^1]` + `[^1]: …`,且原位置不再重复一份。"""
    markdown, blocks = body_page(r"正文里出现一个注释标记 $ ^{①} $，后面还有字。", ["① 这是脚注内容。"])
    fake.start(page_jsonl([(markdown, blocks)]))

    result = convert(tmp_path)

    md = result.book_md.read_text(encoding="utf-8")
    assert "标记 [^1]，后面还有字。" in md
    assert "[^1]: 这是脚注内容。" in md
    assert md.count("这是脚注内容") == 1                 # 不能出现两份(原位置那份已去掉)
    assert "①" not in md
    assert result.stats["footnotes_total"] == 1
    assert result.stats["footnotes_linked"] == 1
    assert result.stats["footnotes_unmatched"] == 0


def test_two_footnotes_keep_their_own_text(tmp_path, fake) -> None:
    markdown, blocks = body_page("正文里甲处①,乙处②,两处都要配上。", ["① 甲的注释。", "② 乙的注释。"])
    fake.start(page_jsonl([(markdown, blocks)]))

    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=1), tmp_path / "work")

    md = result.book_md.read_text(encoding="utf-8")
    assert "甲处[^1],乙处[^2]," in md
    assert "[^1]: 甲的注释。" in md and "[^2]: 乙的注释。" in md
    assert result.stats["footnotes_linked"] == 2


# ---------------------------------------------------------------- 不误伤 / 不串

def test_marker_inside_math_is_not_linked_when_it_creates_ambiguity(tmp_path, fake) -> None:
    """同页两个 `$ ^{①} $`(一个真公式、一个引用)却只有 1 条脚注 → 有歧义,整组不转。"""
    markdown, blocks = body_page(r"公式 $x^{①}$ 与引用 $ ^{①} $ 都在这一页。", ["① 唯一的注释。"])
    fake.start(page_jsonl([(markdown, blocks)]))

    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=1), tmp_path / "work")

    md = result.book_md.read_text(encoding="utf-8")
    assert "[^1]" not in md                              # 不硬猜
    assert r"公式 $x^{①}$ 与引用 $ ^{①} $ 都在这一页。" in md   # 正文一字不改
    assert "① 唯一的注释。" in md                          # 注释安全保留
    assert md.count("唯一的注释") == 1
    assert result.stats["footnotes_linked"] == 0 and result.stats["footnotes_unmatched"] == 1


def test_math_marker_without_any_note_is_left_alone(tmp_path, fake) -> None:
    text = "正文里的公式 $x^{①}$ 不是脚注。"
    fake.start(page_jsonl([(text, [block("text", text, [114, 300, 1070, 360])])]))

    result = convert(tmp_path)

    assert result.book_md.read_text(encoding="utf-8").count("$x^{①}$") == 1
    assert result.stats.get("footnotes_total", 0) == 0


def test_same_marker_on_different_pages_is_not_crossed(tmp_path, fake) -> None:
    """两页各有一个 `①` 引用与一条 `①` 注释 → 各配各的。"""
    fake.start(page_jsonl([body_page("第一页的引用①。", ["① 第一页的注释。"]),
                     body_page("第二页的引用①。", ["① 第二页的注释。"])]))

    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=2), tmp_path / "work")

    md = result.book_md.read_text(encoding="utf-8")
    assert "第一页的引用[^1]。" in md and "第二页的引用[^2]。" in md
    assert md.index("[^1]: 第一页的注释。") < md.index("第二页的引用")
    assert result.stats["footnotes_linked"] == 2 and result.stats["footnotes_unmatched"] == 0


def test_note_without_a_reference_is_kept_as_plain_text(tmp_path, fake) -> None:
    """有注释、没有对应引用 → 原样保留为普通文本,既不删也不重复。"""
    markdown, blocks = body_page("这一页没有引用标记。", ["① 孤立的注释。"])
    fake.start(page_jsonl([(markdown, blocks)]))

    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=1), tmp_path / "work")

    md = result.book_md.read_text(encoding="utf-8")
    assert "① 孤立的注释。" in md and md.count("孤立的注释") == 1
    assert "[^1]" not in md
    assert result.stats["footnotes_unmatched"] == 1


# ---------------------------------------------------------------- 缓存 / resume

def test_structured_blocks_are_cached_with_the_part(tmp_path, fake) -> None:
    """归一化块要随段缓存落盘,否则 resume 时脚注就重建不出来。"""
    markdown, blocks = body_page("这一页正文里有一个引用①。", ["① 注释。"])
    fake.start(page_jsonl([(markdown, blocks)]))

    _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=1), tmp_path / "work")

    cached = list((tmp_path / "work" / "_parts").rglob(CONTENT_LIST_FILE))
    assert len(cached) == 1
    data = json.loads(cached[0].read_text(encoding="utf-8"))
    assert [b["type"] for b in data].count("page_footnote") == 1
    # 页边界也要落盘:有了它,引用定页与脚注落位都按精确偏移走,不再靠文本对齐
    breaks = [b for b in data if b["type"] == "page_break"]
    assert breaks and breaks[0]["offset"] == 0 and breaks[0]["page_idx"] == 0
    assert all({"type", "text", "page_idx", "bbox"} <= set(b) for b in data
               if b["type"] != "page_break")
    # 缓存里存的是**未加脚注**的原始 Markdown(重建在复用时做,规则改进可重算)
    assert "[^1]" not in (tmp_path / "work" / "_parts" / "whole" / "full.md").read_text(encoding="utf-8")


def test_resume_rebuilds_footnotes_without_touching_the_cloud(tmp_path, fake) -> None:
    markdown, blocks = body_page("这一页正文里有一个引用①。", ["① 注释。"])
    fake.start(page_jsonl([(markdown, blocks)]))
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=1)
    work = tmp_path / "work"

    first = _adapter().convert(pdf, work)
    again = _adapter().convert(pdf, work)

    assert fake.counts() == {"posts": 1, "polls": 1, "downloads": 1}   # 第二次没碰云端
    assert again.book_md.read_text(encoding="utf-8") == first.book_md.read_text(encoding="utf-8")
    assert again.stats["footnotes_linked"] == 1


def test_old_cache_without_structured_result_still_merges(tmp_path, fake) -> None:
    """老缓存(没有结构化结果)→ 照常复用,不重新 OCR,不报错,也不硬造脚注。"""
    markdown, blocks = body_page("这一页正文里有一个引用①。", ["① 注释。"])
    fake.start(page_jsonl([(markdown, blocks)]))
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=1)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)

    # 模拟本改动之前落的段缓存:只有 Markdown,没有附带的结构化结果
    for path in (work / "_parts").rglob(CONTENT_LIST_FILE):
        path.unlink()

    again = _adapter().convert(pdf, work)

    assert fake.counts() == {"posts": 1, "polls": 1, "downloads": 1}   # 没有重新提交
    md = again.book_md.read_text(encoding="utf-8")
    assert "① 注释。" in md and md.count("注释") == 1       # 内容在(原始 Markdown 里那份)
    assert "[^1]" not in md                                # 没有结构化结果就不硬配
    assert "footnotes_total" not in again.stats


# ---------------------------------------------------------------- 全链路

def test_paddleocr_footnotes_reach_the_epub_as_native_footnotes(tmp_path, fake) -> None:
    """PaddleOCR → 统一脚注层 → Cleaner → Pandoc → EPUB:产物里是原生脚注。"""
    from epub.pandoc import build_epub
    from markdown.cleaner import clean_markdown

    markdown, blocks = body_page("甲处引用①,乙处引用②,内容足够长以便成段。",
                                 ["① 甲的注释:内容足够长,用来生成真脚注。",
                                  "② 乙的注释:同样足够长,避免被判成空段。"])
    fake.start(page_jsonl([(markdown, blocks)]))
    work = tmp_path / "work"
    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=1), work)

    assert result.stats["footnotes_linked"] == 2
    cleaned = clean_markdown(result.book_md.read_text(encoding="utf-8"))
    assert "[^1]" in cleaned and "[^1]: 甲的注释" in cleaned
    (work / "book.md").write_text(cleaned, encoding="utf-8")

    epub = build_epub(work / "book.md", work, tmp_path / "out", title="PaddleOCR 样书")

    xhtml = ""
    with zipfile.ZipFile(epub) as zf:
        for name in zf.namelist():
            if name.endswith(".xhtml"):
                xhtml += zf.read(name).decode("utf-8", errors="replace")
    assert xhtml.count('epub:type="footnote"') >= 2
    assert xhtml.count("noteref") >= 2
    assert xhtml.count("doc-backlink") >= 2                # 脚注回正文的回链
    assert "甲的注释" in xhtml and "乙的注释" in xhtml
