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
    """裸编号(「六」「六.」)、纯标点(「=」)、以冒号收尾的称呼行都不是章。"""
    md, _ = _run_author_toc()
    heads = [ln for ln in md.split("\n") if ln.startswith("#")]
    assert not [h for h in heads if h.lstrip("#").strip() in {"六", "六.", "=", "負責同志："}]


def test_toc_entry_already_present_as_heading_is_not_reported_missing() -> None:
    """目录条目在正文里本来就是标题:不能报「找不到落点」(误报会让人以为丢章)。"""
    md_in = """\
沉船礁……齐平 3
两个班长……王世阁 34
"鉄笔御史"……王杏元 166

# 沉船礁

正文开头足够长,可以当作锚点用,不是标题。

# 两个班长

第二篇正文同样足够长,可以当作锚点用,不是标题。

# “鉄笔御史”

第三篇正文也足够长,可以当作锚点用,不是标题。
"""
    ev = parse_evidence(_evidence(("text", "与标题无关的一条正文", 0)),
                        backend="mineru", part_id="book")
    md, notes = restore_headings(md_in, ev)
    assert not any("找不到落点" in n for n in notes)
    heads = [ln for ln in md.split("\n") if ln.startswith("#")]
    assert "# “鉄笔御史”" in heads, "正文里已经是标题的章被降级了"


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


# ---------------------------------------------------------------- MinerU 版面正常化(《夜行货车》实测形态)
#
# 合成夹具复刻这本小说集里同时出现的几种错法:目录「页码在前」、页眉把大标题与
# 副标题连写、页眉在 JSON 里排到本页正文之后、单字大标题整个漏识别、分节号被
# 降级后又被页码规则删掉、日期与小节同级。印刷页码 + 5 = 证据层 page_idx。

_NT_TOC = """\
目录

001 六月里的玫瑰花
027 贺大哥
073 夜行货车
153 云
"""

_NT_A = "门开了。像地窖一般幽暗的酒吧,便在一霎时间掠过一片白色的日光,一个又瘦又高的黑人走了进来。"
_NT_D = "贺大哥正文第一段足够长,用来给页眉标题提供落点锚点使用,内容不能太短。"
_NT_B = "摩根索先生跨着大步走过林荣平的办公室,这一段文字必须足够长,才能作为标题落点的锚点使用。"
_NT_C = "赠给敬爱的王祯和兄,并以最虔诚的祝福,祈望他快快康复,这一段也足够长可以当锚点。"

_NT_MD = f"""\
{_NT_TOC}
## 疲倦的月亮

{_NT_A}

## 2

## 3

## 三月十二日

## 1 凡劳苦背重担的人……

{_NT_D}

## 1 长尾雉的标本

{_NT_B}

华盛顿大楼之三

{_NT_C}

## 1 麦园

正文其余部分。
"""


def _nt_block(kind: str, text: str, page: int, box: list, level: int | None = None) -> dict:
    item = {"type": kind, "text": text, "page_idx": page, "bbox": box}
    if level is not None:
        item["text_level"] = level
    return item


def _nt_evidence() -> list[dict]:
    return [
        _nt_block("text", "疲倦的月亮", 6, [406, 449, 578, 473], 2),
        _nt_block("text", _NT_A, 6, [95, 517, 892, 727]),
        _nt_block("header", "六月里的玫瑰花", 6, [95, 112, 360, 139]),
        _nt_block("page_number", "001", 6, [845, 948, 886, 963]),
        _nt_block("text", _NT_D, 32, [95, 517, 892, 600]),
        _nt_block("header", "贺大哥", 32, [101, 114, 215, 139]),
        _nt_block("page_number", "027", 32, [845, 948, 886, 963]),
        _nt_block("text", "1 长尾雉的标本", 78, [360, 445, 613, 469], 2),
        _nt_block("text", _NT_B, 78, [90, 513, 886, 600]),
        _nt_block("header", "夜行货车华盛顿大楼之一", 78, [87, 110, 267, 167]),
        _nt_block("page_number", "073", 78, [837, 944, 883, 961]),
        _nt_block("text", "华盛顿大楼之三", 158, [84, 160, 264, 180]),
        _nt_block("text", _NT_C, 158, [84, 233, 816, 257]),
        _nt_block("text", "1 麦园", 158, [415, 460, 549, 484], 2),
        _nt_block("page_number", "153", 158, [837, 957, 880, 974]),
        # 编号小节/日期/省略号标题的证据放在另一页:它们只要 ``text_level`` 背书,
        # 不能挤进章首那一页的锚点候选(否则篇名会插到本章第 3 段之后)。
        _nt_block("text", "2", 300, [450, 200, 476, 220], 2),
        _nt_block("text", "3", 300, [447, 240, 473, 260], 2),
        _nt_block("text", "三月十二日", 300, [171, 280, 322, 300], 2),
        _nt_block("text", "1 凡劳苦背重担的人……", 300, [290, 320, 680, 340], 2),
    ]


def _nt_run():
    ev = parse_evidence(_nt_evidence(), backend="mineru", part_id="book")
    return restore_headings(_NT_MD, ev)


def test_toc_with_leading_page_numbers_is_authoritative() -> None:
    from pdf2epub.ir.structure import _parse_toc_line, _toc_titles

    assert _parse_toc_line("001 六月里的玫瑰花") == ("六月里的玫瑰花", "", "001")
    assert _parse_toc_line("379 为什么要读陈映真？/赵刚") == (
        "为什么要读陈映真？", "赵刚", "379")
    toc, _ = _toc_titles(_NT_TOC.split("\n"))
    assert [t.page for t in toc.values()] == [1, 27, 73, 153]


def test_numbered_body_lines_are_not_mistaken_for_toc() -> None:
    from pdf2epub.ir.structure import _toc_titles

    toc, _ = _toc_titles(["1 长尾雉的标本", "2 温柔的乳房", "3 沙漠博物馆"])
    assert toc == {}


def test_numbered_ellipsis_title_is_not_a_sentence() -> None:
    from pdf2epub.ir.structure import _sentence_like

    assert not _sentence_like("1 凡劳苦背重担的人……")


def test_split_header_subtitle() -> None:
    from pdf2epub.ir.structure import _split_header_subtitle

    assert _split_header_subtitle("夜行货车华盛顿大楼之一", "夜行货车") == "华盛顿大楼之一"
    assert _split_header_subtitle("万商帝君\n华盛顿大楼之四", "万商帝君") == "华盛顿大楼之四"


def test_page_offset_ignores_repeated_footers() -> None:
    """页脚每页都印篇名(实测「夜行货车」121 次),不能把卷首偏移算成 15。"""
    from pdf2epub.ir.structure import _toc_page_offset, _toc_titles

    ev = parse_evidence(_nt_evidence(), backend="mineru", part_id="book")
    footers = parse_evidence(
        [_nt_block("footer", "夜行货车", p, [162, 944, 258, 963]) for p in range(6, 18)],
        backend="mineru", part_id="book")
    toc, _ = _toc_titles(_NT_TOC.split("\n"))
    assert _toc_page_offset(toc, tuple(ev) + tuple(footers)) == 5


def test_layout_order_restores_merged_and_missing_chapter_titles() -> None:
    md, notes = _nt_run()
    # 篇名必须在本章第一个小节之前(旧逻辑按 JSON 块序,页眉被排到正文之后)
    assert md.index("# 六月里的玫瑰花") < md.index("## 疲倦的月亮")
    # 页眉合并的「大标题 + 副标题」拆开,且都排在二级标题之前
    assert (md.index("# 夜行货车") < md.index("华盛顿大楼之一")
            < md.index("## 1 长尾雉的标本"))
    # 目录点名、正文里一个字都没识别出来的单字大标题,按印刷页码补回
    assert md.index("# 云") < md.index("华盛顿大楼之三")
    assert not any("找不到落点" in n for n in notes)


def test_numbered_sections_survive_and_dates_drop_one_level() -> None:
    md, _ = _nt_run()
    assert "## 2" in md and "## 3" in md                 # 分节号不再被降级+删掉
    assert "## 1 凡劳苦背重担的人……" in md              # 编号+省略号不再被当句子
    assert "### 三月十二日" in md                       # 日期比小节低一级


def _inline_blocks() -> list[dict]:
    """《夜行货车》里真实的问题块:p180 成对署名、p193/p338 英文招牌。"""
    return [
        _nt_block("text", "二哥", 180, [453, 432, 523, 454], 2),
        _nt_block("text", "装配线·文秀英", 180, [555, 471, 750, 493], 2),
        _nt_block("text", "WASHINGTON BUILDING", 193, [209, 375, 584, 395], 2),
        _nt_block("text", "MARKETING MANAGER", 338, [200, 738, 537, 758], 2),
        # 真标题:编号小节、日期、无编号中文小节、裸分节号 —— 一个都不能被误判
        _nt_block("text", "疲倦的月亮", 6, [406, 449, 578, 473], 2),
        _nt_block("text", "2", 39, [450, 449, 476, 469], 2),
        _nt_block("text", "1 长尾雉的标本", 78, [360, 445, 613, 469], 2),
        _nt_block("text", "三月十二日", 170, [171, 631, 322, 653], 2),
        _nt_block("text", "2 ROLANTO", 284, [200, 598, 415, 620], 2),
    ]


def test_inline_display_keys_catch_only_quoted_layout_text() -> None:
    """``text_level`` 只是字号层级:招牌/广告/文章标题+署名不是书的小节。"""
    from pdf2epub.ir.structure import _inline_display_keys, _title_key

    ev = parse_evidence(_inline_blocks(), backend="mineru", part_id="book")
    keys = _inline_display_keys(ev)
    for text in ("二哥", "装配线·文秀英", "WASHINGTON BUILDING", "MARKETING MANAGER"):
        assert _title_key(text) in keys, f"{text} 没被认成内嵌展示文字"
    for text in ("疲倦的月亮", "2", "1 长尾雉的标本", "三月十二日", "2 ROLANTO"):
        assert _title_key(text) not in keys, f"{text} 被误判成内嵌展示文字"


_INLINE_MD = """目录

001 甲篇
027 乙篇
073 丙篇

甲篇正文第一段足够长用来定位章首。

## 疲倦的月亮

## 2

## 1 长尾雉的标本

### 三月十二日

## 2 ROLANTO

乙篇正文第一段足够长用来定位章首。

## 二哥

## 装配线·文秀英

## WASHINGTON BUILDING

## MARKETING MANAGER

小节三的正文第一段足够长用来定位小节。

丙篇正文第一段足够长用来定位章首。
"""


def _inline_evidence() -> list[dict]:
    return [
        _nt_block("header", "甲篇", 6, [95, 112, 360, 139]),
        _nt_block("text", "甲篇正文第一段足够长用来定位章首。", 6, [95, 517, 892, 600]),
        _nt_block("header", "乙篇", 32, [101, 114, 215, 139]),
        _nt_block("text", "乙篇正文第一段足够长用来定位章首。", 32, [95, 517, 892, 600]),
        _nt_block("header", "丙篇", 78, [87, 110, 267, 167]),
        _nt_block("text", "丙篇正文第一段足够长用来定位章首。", 78, [90, 513, 886, 600]),
        *_inline_blocks(),
        # 页首编号页眉小节:目录不列,但确实是《云》章的第 3 小节
        _nt_block("header", "3 AMERICAN DREAM", 48, [308, 112, 665, 132]),
        _nt_block("text", "小节三的正文第一段足够长用来定位小节。", 48, [90, 178, 883, 240]),
    ]


def test_inline_titles_demoted_and_numbered_header_section_restored() -> None:
    ev = parse_evidence(_inline_evidence(), backend="mineru", part_id="book")
    md, _ = restore_headings(_INLINE_MD, ev)
    heads = [line for line in md.split("\n") if line.startswith("#")]

    assert "# 甲篇" in heads and "# 乙篇" in heads and "# 丙篇" in heads
    assert "## 3 AMERICAN DREAM" in heads          # 章内二级小节,不与章并列
    assert "# 3 AMERICAN DREAM" not in heads
    assert (md.index("## 2 ROLANTO") < md.index("## 3 AMERICAN DREAM")
            < md.index("丙篇正文"))
    for text in ("二哥", "装配线·文秀英", "WASHINGTON BUILDING", "MARKETING MANAGER"):
        assert not any(h.endswith(" " + text) for h in heads), f"{text} 仍是标题"
        assert f"\n{text}\n" in md, f"{text} 降级后文字丢了"
    assert "## 2" in heads and "## 1 长尾雉的标本" in heads
    assert "### 三月十二日" in heads and "## 2 ROLANTO" in heads


def test_footnote_star_is_stripped_from_promoted_title() -> None:
    md_in = ("目录\n\n001 甲篇\n027 乙篇\n379 为什么要读陈映真？\n\n"
             "为什么要读陈映真？\\*\n\n赵刚\n\n正文足够长,以免被当成诗行。\n")
    ev = parse_evidence(_evidence(("text", "与标题无关的一条正文", 1)),
                        backend="mineru", part_id="book")
    md, _ = restore_headings(md_in, ev)
    assert "# 为什么要读陈映真？" in md
    assert "# 为什么要读陈映真？\\*" not in md


def test_variant_chapters_adopted_among_many_sections() -> None:
    """真实短篇集:目录印简体、正文 OCR 成繁体异体,窗口里还夹着多个 L2 小节。

    旧判据「前后目录条目之间恰好剩一个标题」被 3–6 个小节挤掉,两个真章标题
    一直挂在上一篇底下。证据层把章标题标成 text_level=1、章内小节标成 2,
    窗口里 L1 唯一才认 —— 既有强信号,也不靠猜。
    """
    md_in = """\
目次

机场上的故事……官伟勋 68
巡线工……刘安琪 86
路考……张天民 102
骏马飞驰……王慧芹 121
王林林……黄树芳 137

# 机场上的故事

## 2

## 3

## 巡 綫 工

## 4

## 5

# 路 考

## 6

## 駿馬飞馳

# 王林林
"""

    def blk(text: str, page: int, level: int) -> dict:
        return {"type": "text", "text": text, "page_idx": page,
                "bbox": [400, 310, 650, 340], "text_level": level}

    rows = [
        blk("机场上的故事", 70, 1), blk("巡 綫 工", 88, 1),
        blk("路 考", 104, 1), blk("駿馬飞馳", 123, 1), blk("王林林", 139, 1),
        blk("2", 75, 2), blk("3", 77, 2), blk("4", 90, 2),
        blk("5", 92, 2), blk("6", 110, 2),
    ]
    md, notes = _run(md_in, rows)
    heads = [ln for ln in md.split("\n") if ln.startswith("#")]
    assert "# 巡 綫 工" in heads and "# 駿馬飞馳" in heads
    assert "## 2" in heads and "## 6" in heads, "章内小节不该被一起提升"
    assert (md.index("# 机场上的故事") < md.index("# 巡 綫 工")
            < md.index("# 路 考") < md.index("# 駿馬飞馳")
            < md.index("# 王林林"))
    assert any("补认 2" in n for n in notes)
