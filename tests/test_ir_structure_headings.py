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

from pdf2epub.ir.parse import parse_evidence
from pdf2epub.ir.structure import restore_headings
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
            ("header", "第一章 起头", 1),
            ("text", BODY_A, 1),
            ("text", "2", 2),
            ("heading", DIALOGUE, 2, MID),
            ("header", "第二章 续篇", 3),
            ("text", BODY_B, 3),
            ("text", BODY_C, 5),
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
    from pdf2epub.ir import build_ir, render_ir
    md, _ = _run()
    ir = build_ir(md, source="synthetic.md", backend="mineru", total_pages=5)
    assert render_ir(ir) == md

def test_title_block_listed_after_its_own_body_is_still_inserted_on_top() -> None:
    """后端把 ``header`` 排在本页正文之后(实测 MinerU 常这样),标题仍要落在正文之前。

    这是「目录对了、标题却插进正文中间」的根因:按块列表顺序取「标题之后的下一条」
    会取到本章第二段甚至下一页,于是标题前留下几段正文。
    """
    rows = _evidence(
        ("header", "第一章 起头", 1, [10.0, 20.0, 300.0, 40.0]),   # 标题在页首
        ("text", BODY_A, 1, [10.0, 400.0, 300.0, 440.0]),         # 但块列表里排在标题之前
        ("text", BODY_B, 1, [10.0, 600.0, 300.0, 640.0]),
        ("text", "2", 2),
        ("header", "第二章 续篇", 3),
        ("text", BODY_C, 3, [10.0, 400.0, 300.0, 440.0]),
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
        ("header", "为了超级明星", 1, [10.0, 20.0, 300.0, 40.0]),
        ("text", short, 1, [10.0, 400.0, 300.0, 440.0]),
        ("text", long_, 1, [10.0, 600.0, 300.0, 640.0]),
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


def test_heading_restore_shares_one_before_after_snapshot(tmp_path) -> None:
    """标题裁决属于「本次清理」:报告的字数口径与 outcome 必须是同一份 before/after。

    修复前 ``md_before`` 快照取在裁决之前,outcome 却取在之后 —— 只补标题、没有
    Patch 的一轮里 ``changed`` 为 False,补回的标题**从来没写进 book.md**;报告又
    显示字数凭空增加,撤销时也算不回来。
    """
    from pdf2epub.clean.pipeline import clean_book_md
    path = tmp_path / "book.md"
    path.write_text(MD, encoding="utf-8")
    outcome = clean_book_md(path, backend="mineru", evidence=_evidence(
        ("header", "第一章 起头", 1),
        ("text", BODY_A, 1),
        ("text", "2", 2),
        ("heading", DIALOGUE, 2, MID),
        ("header", "第二章 续篇", 3),
        ("text", BODY_B, 3),
        ("text", BODY_C, 5),
    ))

    assert "# 第一章 起头" in outcome.md_after
    assert outcome.report.stats.chars_before == len(outcome.md_before)
    assert outcome.report.stats.chars_after == len(outcome.md_after)
    if outcome.changed:                       # 写回的内容必须与报告里的 after 一致
        assert path.read_text(encoding="utf-8") == outcome.md_after


# ---------------------------------------------------------------- 目录带作者列(短篇集实测形态)

_TOC_WITH_AUTHOR = """\
沉船礁……齐平 3
两个班长……王世阁 34
"鉄笔御史"……王杏元 166
"""

MD_WITH_AUTHOR = f"""\
# 书名

目次

{_TOC_WITH_AUTHOR}
# 沉船礁

# 齐平

副艦长楊亭山交完班,順着鉄梯輕快地下了指揮台,心里想着海上的臭咸魚味。

## =

# 两个班长

梁柏林决定先走一步棋:在邓金正式被任命当班长之前,找他谈谈。

## 六

# "鉄笔御史"

杏林村过去也叫三类村,怎么一轉眼就这样好呢？

## 負責同志：

# 別处的短篇

这一篇不在目次里,只是章内的小节名被后端标成了标题。
"""


def _run_author_toc(md: str = MD_WITH_AUTHOR):
    rows = _evidence(
        ("header", "书名", 0),
        ("text", "沉船礁", 1),
        ("text", "齐平", 1),
    )
    ev = parse_evidence(rows, backend="mineru", part_id="book")
    return restore_headings(md, ev)


def test_toc_with_author_column_is_parsed() -> None:
    """目录印成「标题……作者 页码」:作者列必须读出来,否则整页目录一条都认不出。"""
    from pdf2epub.ir.structure import _parse_toc_line

    assert _parse_toc_line("沉船礁……齐平 3") == ("沉船礁", "齐平", "3")
    assert _parse_toc_line("第一章 绪论 35") == ("第一章 绪论", "", "35")
    assert _parse_toc_line("这一行是正文,不是目录条目。") is None


def test_toc_authors_are_not_chapters() -> None:
    """目录作者栏里的人名成了标题 → 降回正文(实测每篇的署名都成了一个章)。"""
    md, notes = _run_author_toc()
    assert "\n齐平\n" in md and "# 齐平" not in md
    assert any("降级" in n for n in notes)


def test_bare_numbers_and_salutations_are_not_chapters() -> None:
    """裸编号(「六」)、纯标点(「=」)、以冒号收尾的称呼行都不是章。"""
    md, _ = _run_author_toc()
    for fake in ("## 六", "# 六", "# =", "## 負責同志："):
        assert fake not in md


def test_toc_is_authority_for_non_listed_headings() -> None:
    """目录是权威:目录之外的真标题降为章内小节(不再与章并列)。"""
    md, _ = _run_author_toc()
    heads = [ln for ln in md.split("\n") if ln.startswith("#")]
    assert "## 別处的短篇" in heads
    assert "# 別处的短篇" not in heads


def test_chapter_with_variant_characters_is_adopted_by_toc_order() -> None:
    """繁简/异体导致文字对不上时,按目录顺序补认(窗口里恰好一个标题才认)。

    实测形态:目录印「巡线工」,正文 OCR 成「巡 綫 工」—— 文字比对永远配不上,
    于是真章标题被降成小节、挂在上一篇底下。判据只有位置:目录前后两条都找到了
    落点,中间恰好只剩一个标题,它就是这条目录对应的章。
    """
    md_in = """\
沉船礁……齐平 3
巡线工……刘安琪 86
路考……张天民 102

# 沉船礁

正文开头足够长,可以当作锚点用,不是标题。

# 巡 綫 工

这一篇的正文也足够长,可以当作锚点用,不是标题。

# 路 考

第三篇正文同样足够长,可以当作锚点用,不是标题。
"""
    ev = parse_evidence(_evidence(("text", "与标题无关的一条正文", 0)),
                        backend="mineru", part_id="book")
    md, notes = restore_headings(md_in, ev)
    heads = [ln for ln in md.split("\n") if ln.startswith("#")]
    assert "# 巡 綫 工" in heads, "字形不同的章标题被降成了小节"
    assert any("补认" in n for n in notes)
