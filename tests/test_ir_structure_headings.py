"""标题裁决:按证据层/目录页补回丢失的章标题、降级无出处的伪标题。

这一层解决的是「证据层有、正文没有」和反向的「正文有、证据层不认」两件事:

  - 后端把章标题判成页眉 → Markdown 里一行都不写,靠证据页号找回落点插回;
  - 后端把对话/裸数字标成标题 → 证据说它是正文,降回段落;
  - 目录页给标题但证据层没有 → 正文里独立成段的那一行就地提升;
  - 书眉(同名页眉出现在 ≥3 页)不当标题;
  - 证据不足(标题来源 < 3)时不降级 —— 无法证明不等于该删。

夹具全部是合成文本与合成 ``content_list``。
"""
from __future__ import annotations

from ir.parse import parse_evidence
from ir.structure import restore_headings

TOC = """\
目录

第一章 起头 / 1
第二章 续篇 / 9
附录: 附记 / 20
"""

BODY_A = "第一章正文,内容足够长,第一段前二十四个字要够长才能当锚点用。"
BODY_B = "第二章正文,同样足够长,第一段前二十四个字也要够长才行。"
BODY_C = "附录正文,足够长,单独一段,用来确认提升后的标题位置。"
DIALOGUE = "又怎么了?"

MD = f"""\
# 书名页

{TOC}

{BODY_A}

第一章的第二段,提到一些别的细节,内容也不短。

## 2

## {DIALOGUE}

{BODY_B}

附录: 附记

{BODY_C}
"""


TOP = [10.0, 20.0, 300.0, 40.0]        # 页首
MID = [100.0, 600.0, 300.0, 640.0]     # 页中(句子中间那种强调/对白)


def _evidence(*rows):
    """``(kind, text, page)`` 或 ``(kind, text, page, bbox)``。"""
    out = []
    for row in rows:
        kind, text, page = row[:3]
        box = row[3] if len(row) > 3 else TOP
        item = {"type": kind, "text": text, "page_idx": page, "bbox": box}
        if kind == "heading":          # MinerU 用 text_level 表达层级,"heading" 不是它的 type
            item["type"], item["text_level"] = "text", 2
        out.append(item)
    return out


def _run(md=MD, rows=None):
    evidence = parse_evidence(
        rows if rows is not None else _evidence(
            ("header", "第一章 起头", 0),
            ("text", BODY_A, 0),
            ("text", "2", 1),
            ("heading", DIALOGUE, 1, MID),
            ("header", "第二章 续篇", 2),
            ("text", BODY_B, 2),
            ("text", BODY_C, 4),
        ),
        backend="mineru", part_id="book",
    )
    return restore_headings(md, evidence)


def test_missing_chapter_titles_are_restored_in_place() -> None:
    md, notes = _run()
    assert "# 第一章 起头\n\n" + BODY_A in md
    assert "# 第二章 续篇\n\n" + BODY_B in md
    assert any("补回" in n for n in notes)


def test_toc_only_title_is_promoted_where_it_stands() -> None:
    """目录里有、证据层没有的标题:正文里独立成段的那一行就地提升(不打乱顺序)。"""
    md, _ = _run()
    assert "# 附录: 附记" in md
    # 提升不是插入:附录正文仍然紧跟在它后面
    assert "# 附录: 附记\n\n" + BODY_C in md


def test_fake_headings_demoted_when_evidence_says_body() -> None:
    md, notes = _run()
    assert "\n2\n" in md and "## 2" not in md
    assert f"\n{DIALOGUE}\n" in md and f"## {DIALOGUE}" not in md
    assert any("降级" in n for n in notes)


def test_running_head_is_not_a_title() -> None:
    """同名页眉出现在 ≥3 页 = 书眉,不补成标题。"""
    rows = _evidence(
        ("header", "某某文集", 0),
        ("text", BODY_A, 0),
        ("header", "某某文集", 1),
        ("text", BODY_B, 1),
        ("header", "某某文集", 2),
        ("text", BODY_C, 2),
    )
    md, _ = _run(rows=rows)
    assert "某某文集" not in md


def test_no_demotion_without_enough_title_sources() -> None:
    """只有 1 条标题来源时不降级:「无法证明是标题」不等于「该删」。"""
    rows = _evidence(
        ("header", "第一章 起头", 0),
        ("text", BODY_A, 0),
        ("text", "2", 1),
        ("text", BODY_B, 2),
    )
    md, _ = _run(md=MD.replace(TOC, "目录"), rows=rows)
    assert "## 2" in md


def test_no_evidence_leaves_md_untouched() -> None:
    md, notes = restore_headings(MD, [])
    assert md == MD
    assert notes == ()


def test_restored_md_still_parses_and_keeps_roundtrip() -> None:
    """插入/降级只动标题行,不该破坏 IR 的往返一致性。"""
    from ir import build_ir, render_ir

    md, _ = _run()
    ir = build_ir(md, source="synthetic.md", backend="mineru", total_pages=5)
    assert render_ir(ir) == md

def test_title_block_listed_after_its_own_body_is_still_inserted_on_top() -> None:
    """后端把 ``header`` 排在本页正文之后(实测 MinerU 常这样),标题仍要落在正文之前。

    这是「目录对了、标题却插进正文中间」的根因:按块列表顺序取「标题之后的下一条」
    会取到本章第二段甚至下一页,于是标题前留下几段正文。
    """
    rows = _evidence(
        ("header", "第一章 起头", 0),                      # 标题在第 1 页页首
        ("text", BODY_A, 0, [10.0, 400.0, 300.0, 440.0]),   # 但块列表里排在标题之前
        ("text", BODY_B, 0, [10.0, 600.0, 300.0, 640.0]),
        ("text", "2", 1),
        ("header", "第二章 续篇", 2),
        ("text", BODY_C, 2, [10.0, 400.0, 300.0, 440.0]),
    )
    md, _ = _run(rows=rows)
    nl2 = "\n\n"

    # 标题必须紧贴在**本页第一条正文**之前,而不是几段之后
    assert md.index("# 第一章 起头") < md.index(BODY_A)
    assert "# 第一章 起头" + nl2 + BODY_A in md

def test_short_opening_paragraph_is_used_as_anchor() -> None:
    """章首句很短(实测 16 字)时也要能用:门槛过高会把整段跳过,标题就晚一段。"""
    short = "老金见到那个孩子是在一个夏夜。"
    long_ = "自从来到首尔,老金很少能睡一个安稳觉,要么被儿子的长途电话惊醒。"
    rows = _evidence(
        ("header", "为了超级明星", 0, [10.0, 20.0, 300.0, 40.0]),
        ("text", short, 0, [10.0, 400.0, 300.0, 440.0]),
        ("text", long_, 0, [10.0, 600.0, 300.0, 640.0]),
    )
    md_in = "无关前言。\n\n" + short + "\n\n" + long_ + "\n"
    md, _ = _run(md=md_in, rows=rows)
    nl2 = "\n\n"
    assert "# 为了超级明星" + nl2 + short in md


_BODY_SHAOZHI = "烧纸短篇的第一段正文,内容足够长,能被锚点匹配上。"

def test_book_title_heading_is_demoted_when_a_chapter_shares_the_name() -> None:
    """书名页的 ``# 书名`` 与同名章节撞 id,pandoc 会让两条目录项指向同一处。"""
    md_in = "# 烧纸\n\n著者信息\n\n" + _BODY_SHAOZHI + "\n"
    rows = _evidence(
        ("header", "烧纸", 1, [10.0, 20.0, 300.0, 40.0]),
        ("text", _BODY_SHAOZHI, 1,
         [10.0, 400.0, 300.0, 440.0]),
    )
    ev = parse_evidence(rows, backend="mineru", part_id="book")
    md, _ = restore_headings(md_in, ev, title_hint="烧纸")
    assert md.count("# 烧纸") == 1          # 只保留章标题(后面那个)
    assert md.lstrip().startswith("烧纸")   # 书名页那行降成正文
