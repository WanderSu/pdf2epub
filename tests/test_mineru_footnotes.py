"""结构化脚注 → Pandoc footnote 重建(离线单测)。

覆盖:单脚注 / 多脚注 / 同页重复编号 / 相邻页(跨页)脚注 / OCR 上标变体 /
证据不足不转换 / 有歧义不转换 / 页窗不成立整组不转 / 行首与代码块里的 marker 不算引用 /
编号跨分片稳定 / 结构化结果读取容错。

被测的是 `markdown.footnotes` 的纯函数(不发网络请求);整条链路(解包 → 落盘 → 合并 →
清理 → Pandoc → EPUB)在 `test_ocr_sharding_resume.py` 与 `test_epub_content.py` 里验。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from markdown.footnotes import (HIGH, MEDIUM, block_page_spans, load_content_list,
                                match_footnotes, reconstruct_footnotes, scan_refs,
                                structured_footnotes)

BODY = "这一行正文足够长,用来让内容块与 Markdown 对齐成功。"


def text_block(text: str, page_idx: int, y0: int = 200) -> dict:
    return {"type": "text", "text": text, "page_idx": page_idx,
            "bbox": [80, y0, 900, y0 + 20]}


def note_block(text: str, page_idx: int, y0: int = 790) -> dict:
    """页脚脚注块:真实书里 y0 都在 750 以上(页面底部)。"""
    return {"type": "page_footnote", "text": text, "page_idx": page_idx,
            "bbox": [80, y0, 900, y0 + 20]}


def build(pages: dict[int, tuple[str, list[str]]]) -> tuple[str, list[dict]]:
    """按 {页: (正文, [脚注…])} 造一份 markdown + content_list。"""
    lines: list[str] = []
    blocks: list[dict] = []
    for page in sorted(pages):
        body, notes = pages[page]
        lines.append(body)
        blocks.append(text_block(body, page))
        for i, note in enumerate(notes):
            blocks.append(note_block(note, page, y0=780 + i * 20))
    return "\n\n".join(lines) + "\n", blocks


# ---------------------------------------------------------------- 单 / 多脚注

def test_single_footnote_becomes_pandoc_footnote() -> None:
    md, blocks = build({0: (f"{BODY}引用在这里①。", ["① 第一条注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 1 and res.stats[HIGH] == 1
    assert "引用在这里[^1]。" in res.md
    assert "\n\n[^1]: 第一条注释。" in res.md
    assert "①" not in res.md                  # marker 与注释行里的标记都换掉了


def test_two_footnotes_are_not_collapsed() -> None:
    md, blocks = build({0: (f"{BODY}第一处①,第二处②。", ["① 甲注释。", "② 乙注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 2 and res.stats[HIGH] == 2
    assert "第一处[^1],第二处[^2]。" in res.md
    assert "[^1]: 甲注释。" in res.md and "[^2]: 乙注释。" in res.md


def test_same_marker_on_different_pages_is_not_crossed() -> None:
    """第 1 页与第 2 页都出现 `①` —— 必须各配各的,不能全指向同一条注释。"""
    md, blocks = build({0: (f"{BODY}第一页的引用①。", ["① 第一页的注释。"]),
                        1: (f"{BODY}第二页的引用①。", ["① 第二页的注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 2 and res.stats[HIGH] == 2
    assert res.md.index("[^1]: 第一页的注释。") < res.md.index("第二页的引用")
    assert res.md.index("第二页的引用[^2]。") < res.md.index("[^2]: 第二页的注释。")


def test_footnote_on_next_page_is_medium_confidence() -> None:
    """正文在第 N 页、脚注在第 N+1 页(MinerU 的正文块可能跨页)→ 相邻页 = medium。"""
    md, blocks = build({0: (f"{BODY}引用在这里①。", []),
                        1: (f"{BODY}下一页的正文。", ["① 相邻页的注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 1 and res.stats[MEDIUM] == 1
    assert "引用在这里[^1]。" in res.md
    assert "[^1]: 相邻页的注释。" in res.md


# ---------------------------------------------------------------- 不转换的情形

def test_ambiguous_refs_on_the_same_page_are_not_linked() -> None:
    """同一页有 2 个 `①` 却只有 1 条 `①` 注释 → 有歧义,不转换(内容保留)。"""
    md, blocks = build({0: (f"{BODY}第一处①,后面还有一处①。", ["① 唯一的注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 0
    assert res.stats["unmatched"] == 1 and res.stats["orphan_refs"] == 2
    assert "第一处①,后面还有一处①。" in res.md        # 引用不动
    assert "① 唯一的注释。" in res.md                   # 注释以普通文本留在原处


def test_page_window_violation_keeps_notes_as_plain_text() -> None:
    """引用与脚注隔了 5 页 → 页窗不成立,整组不转;脚注内容不能丢。"""
    md, blocks = build({0: (f"{BODY}引用在这里①。", []),
                        5: (f"{BODY}很远的一页。", ["① 很远的注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 0 and res.stats["unmatched"] == 1
    assert "引用在这里①。" in res.md
    assert "① 很远的注释。" in res.md


def test_note_without_any_reference_stays_plain_text() -> None:
    md, blocks = build({0: (f"{BODY}这一段没有引用标记。", ["① 孤立的注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 0 and res.stats["unmatched"] == 1
    assert res.stats["orphan_refs"] == 0
    assert "① 孤立的注释。" in res.md


# ---------------------------------------------------------------- marker 形态

def test_ocr_superscript_variant_is_recognised() -> None:
    """PaddleOCR 会把 marker 包成行内公式:`$ ^{①} $`。"""
    md, blocks = build({0: (f"{BODY}引用在这里 $ ^{{①}} $。", ["① 注释。"])})

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["linked"] == 1
    assert "引用在这里 [^1]。" in res.md
    assert "[^1]: 注释。" in res.md


def test_line_initial_marker_is_not_a_reference() -> None:
    """行首的 `①` 是注释自己的标记,不是正文引用。"""
    md, _blocks = build({0: (f"{BODY}这一段没有引用。", ["① 孤立的注释。"])})
    assert scan_refs(md, []) == []


def test_marker_inside_code_fence_is_not_a_reference() -> None:
    md = f"```\n{BODY}不是引用①\n```\n\n{BODY}正文里的引用①。\n"
    blocks = [text_block(f"{BODY}正文里的引用①。", 0), note_block("① 注释。", 0)]

    res = reconstruct_footnotes(md, blocks)

    assert res.stats["refs"] == 1 and res.stats["linked"] == 1
    assert "不是引用①" in res.md


# ---------------------------------------------------------------- 编号与稳定性

def test_ids_continue_across_parts() -> None:
    """分片各转一次时编号必须接着走,否则合并后 `[^1]` 会撞号。"""
    md_a, blocks_a = build({0: (f"{BODY}甲引用①。", ["① 甲的注释。"])})
    md_b, blocks_b = build({0: (f"{BODY}乙引用①。", ["① 乙的注释。"])})

    first = reconstruct_footnotes(md_a, blocks_a)
    second = reconstruct_footnotes(md_b, blocks_b, next_id=first.next_id)

    assert "[^1]: 甲的注释。" in first.md
    assert "[^2]: 乙的注释。" in second.md
    assert second.next_id == 3


def test_rerun_is_stable() -> None:
    md, blocks = build({0: (f"{BODY}引用①。", ["① 注释。"])})
    assert reconstruct_footnotes(md, blocks).md == reconstruct_footnotes(md, blocks).md


def test_no_structured_notes_leaves_markdown_untouched() -> None:
    md, blocks = build({0: (f"{BODY}引用①。", [])})
    res = reconstruct_footnotes(md, blocks)
    assert res.md == md and res.next_id == 1 and res.stats["notes"] == 0


# ---------------------------------------------------------------- 结构化结果读取

def test_structured_footnotes_keeps_page_and_marker() -> None:
    notes = structured_footnotes([text_block("正文。", 3), note_block("① 注释甲。", 3),
                                  note_block("② 注释乙。", 4, y0=800)])
    assert [(n.marker, n.page_idx) for n in notes] == [("①", 3), ("②", 4)]
    assert notes[0].body == "注释甲。"


def test_load_content_list_accepts_v1_and_v2(tmp_path: Path) -> None:
    v1 = tmp_path / "a_content_list.json"
    v1.write_text(json.dumps([note_block("① v1。", 0)], ensure_ascii=False), encoding="utf-8")
    v2 = tmp_path / "b_content_list_v2.json"
    v2.write_text(json.dumps([[{"type": "page_footnote",
                                "content": {"page_footnote_content": [
                                    {"type": "text", "content": "① v2。"}]}}]]),
                  encoding="utf-8")

    assert load_content_list(v1)[0]["type"] == "page_footnote"
    assert structured_footnotes(load_content_list(v2))[0].body == "v2。"


@pytest.mark.parametrize("content", ["{ 不是 JSON", "{}", "[]"])
def test_load_content_list_tolerates_broken_or_empty(content: str, tmp_path: Path) -> None:
    path = tmp_path / "content_list.json"
    path.write_text(content, encoding="utf-8")
    assert load_content_list(path) == []
    assert load_content_list(tmp_path / "缺失.json") == []


def test_match_footnotes_reports_stats() -> None:
    md, blocks = build({0: (f"{BODY}引用①。", ["① 注释。"])})
    refs = scan_refs(md, block_page_spans(md, blocks))
    links, unmatched, orphan, stats = match_footnotes(refs, structured_footnotes(blocks))
    assert len(links) == 1 and not unmatched and not orphan
    assert stats == {"refs": 1, "notes": 1, "groups_refused": 0, "high": 1, "medium": 0}


# ---------------------------------------------------------------- 全链路:结构化结果 → EPUB 原生脚注

def test_structured_footnotes_reach_the_epub_as_native_footnotes(tmp_path: Path) -> None:
    """结构化结果 → 重建 → Cleaner → Pandoc → EPUB:产物里必须是**原生脚注**。

    验收看产物侧,不看 book.md:EPUB 里要有 `epub:type="footnote"` 的脚注块、
    正文侧的 `noteref` 引用,以及脚注回正文的回链。
    """
    import zipfile

    from epub.pandoc import build_epub as pandoc_build_epub
    from markdown.cleaner import clean_markdown

    md, blocks = build({
        0: (f"{BODY}甲处引用①,乙处引用②。", ["① 甲的注释:内容足够长,用来生成真脚注。",
                                              "② 乙的注释:同样足够长,避免被判成空段。"]),
    })
    rebuilt = reconstruct_footnotes(md, blocks)
    assert rebuilt.stats["linked"] == 2

    cleaned = clean_markdown(rebuilt.md)                 # Cleaner 不能把生成的脚注打散
    assert "[^1]" in cleaned and "[^1]: 甲的注释" in cleaned

    work = tmp_path / "work"
    work.mkdir()
    (work / "book.md").write_text(cleaned, encoding="utf-8")
    # 走生产的 EPUB 生成路径(含产物侧补回链那一步),不用测试里的 pandoc 直调
    epub = pandoc_build_epub(work / "book.md", work, tmp_path / "out", title="脚注样书")

    xhtml = ""
    with zipfile.ZipFile(epub) as zf:
        for name in zf.namelist():
            if name.endswith(".xhtml"):
                xhtml += zf.read(name).decode("utf-8")

    assert xhtml.count('epub:type="footnote"') >= 2
    assert xhtml.count("noteref") >= 2                   # 正文引用可点击
    assert 'href="#fn1"' in xhtml and "fnref" in xhtml   # 引用侧落着 fnref 锚点
    assert xhtml.count("doc-backlink") >= 2              # 脚注侧回链(pandoc 不写,产物侧补)
    assert "甲的注释" in xhtml and "乙的注释" in xhtml
