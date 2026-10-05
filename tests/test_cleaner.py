"""清理器规则单测(计划 1.3)。

每个启发式规则都要有「该改的」和「不该改的」两类用例 —— 误伤比漏改更严重。
"""
from __future__ import annotations

import pytest

from markdown.cleaner import (
    CLEAN_KEYS,
    CleanOptions,
    clean_markdown,
    normalize_clean_keys,
    resolve_options,
)


def clean(text: str, **kwargs) -> str:
    """按指定开关清理文本(未指定的项用默认值)。"""
    return clean_markdown(text, options=CleanOptions(**kwargs))


def only(text: str, **enabled: bool) -> str:
    """只开启指定项,其余全关 —— 用来隔离单条规则的行为。"""
    opts = CleanOptions(**{k: False for k in CLEAN_KEYS})
    for k, v in enabled.items():
        setattr(opts, k, v)
    return clean_markdown(text, options=opts)


# ---------------------------------------------------------------- 页眉页脚

def test_running_head_removed_when_repeated() -> None:
    md = "\n\n".join(["书名的页眉", "第一章正文,句号结尾。",
                      "书名的页眉", "第二段正文,句号结尾。",
                      "书名的页眉", "第三段正文,句号结尾。"])
    out = only(md, running_heads=True)
    assert "书名的页眉" not in out
    assert "第三段正文,句号结尾。" in out


def test_running_head_kept_when_rare() -> None:
    md = "书名的页眉\n\n第一章正文,句号结尾。\n\n第二段正文,句号结尾。"
    assert "书名的页眉" in only(md, running_heads=True)


def test_running_head_does_not_touch_repeated_sentences() -> None:
    """重复但以句末标点结尾的行是正文,不删。"""
    md = "\n\n".join(["这是完整句子。" for _ in range(5)])
    assert only(md, running_heads=True).count("这是完整句子。") == 5


def test_running_head_does_not_touch_headings_or_long_lines() -> None:
    md = "\n\n".join(["## 同一标题" for _ in range(4)]
                     + ["这是一条很长的行,长度超过四十个字符的阈值所以不会被当成页眉页脚候选行,即便重复也不会删除。" for _ in range(4)])
    out = only(md, running_heads=True)
    assert out.count("## 同一标题") == 4
    assert out.count("这是一条很长的行") == 4


def test_running_head_disabled() -> None:
    md = "\n\n".join(["书名的页眉", "正文,句号结尾。"] * 3)
    assert "书名的页眉" in only(md)


# ---------------------------------------------------------------- OCR 空格

def test_ocr_spaces_merged_in_chinese_line() -> None:
    out = only("这里提到 Py Mu PDF 这个库。", ocr_spaces=True)
    assert out == "这里提到 PyMuPDF 这个库。"


def test_ocr_spaces_keeps_normal_english_phrase() -> None:
    out = only("This is the cat sat on the mat.", ocr_spaces=True)
    assert out == "This is the cat sat on the mat."


def test_ocr_spaces_keeps_english_phrase_inside_chinese_line() -> None:
    """中文行里的正常英文短语(片段不短)不动。"""
    out = only("原文写着 the cat sat 三个词。", ocr_spaces=True)
    assert "the cat sat" in out


def test_ocr_spaces_keeps_two_words() -> None:
    out = only("参见 Py Mu 库。", ocr_spaces=True)
    assert "Py Mu" in out


# ---------------------------------------------------------------- 重复标题

def test_adjacent_duplicate_heading_deduped() -> None:
    out = only("# 第一章\n\n# 第一章\n\n正文,句号结尾。", dup_headings=True)
    assert out.count("# 第一章") == 1


def test_same_heading_far_apart_kept() -> None:
    md = "## 小结\n\n第一段正文,句号结尾。\n\n## 小结\n\n第二段正文,句号结尾。"
    out = only(md, dup_headings=True)
    assert out.count("## 小结") == 2


def test_heading_with_different_level_kept() -> None:
    out = only("# 第一章\n\n## 第一章\n\n正文,句号结尾。", dup_headings=True)
    assert out.count("第一章") == 2


# ---------------------------------------------------------------- 空标题与层级

def test_empty_heading_dropped() -> None:
    out = only("#\n\n正文,句号结尾。", headings=True)
    assert not out.startswith("#")


def test_heading_level_jump_flattened() -> None:
    out = only("# 第一章\n\n### 小节\n\n正文,句号结尾。", headings=True)
    assert "## 小节" in out


def test_first_heading_promoted_to_h1() -> None:
    out = only("## 第一章\n\n正文,句号结尾。", headings=True)
    assert out.startswith("# 第一章")


def test_heading_text_untouched() -> None:
    md = "# 第一章\n\n## 小节\n\n正文,句号结尾。"
    assert only(md, headings=True) == md


# ---------------------------------------------------------------- 代码块保护

#: 代码块里的每一行都是「会被规则误伤」的形状:纯数字行、重复短行、中文间空格、
#: 行尾空白、`#` 开头的注释、缩进。
HAZARDOUS_FENCE = (
    "```text\n"
    "123\n"
    "\n"
    "32\n"
    "端口 8080 与 主机 名\n"
    "注释   缩进保留\n"
    "end\n"
    "end\n"
    "end\n"
    "###\n"
    "#### 注释\n"
    "尾部空白   \n"
    "```"
)


def test_fenced_code_is_byte_identical() -> None:
    """围栏代码块逐字节不变(页码/页眉/空格/标题/行尾空白规则都不得打进代码)。"""
    md = f"{HAZARDOUS_FENCE}\n\n正文 中文 之间 有空 格。\n"
    out = clean(md)
    assert HAZARDOUS_FENCE in out
    assert "正文中文之间有空格。" in out


def test_fenced_code_keeps_digit_lines() -> None:
    """代码块里的独立数字行不是页码。"""
    out = clean("```text\n123\n\n32\n```\n")
    assert out == "```text\n123\n\n32\n```"


def test_fenced_code_keeps_repeated_short_lines() -> None:
    """代码块里重复出现的短行不是页眉页脚。"""
    out = clean("```text\nend\nend\nend\n```\n\n正文一,句号结尾。\n\n正文二,句号结尾。\n\n正文三,句号结尾。\n")
    assert out.count("end") == 3


def test_fenced_code_headings_untouched() -> None:
    """代码块里的 `#` 注释不是标题(空标题删除 / 层级收敛都不得生效)。"""
    out = clean("```text\n# 注释\n###\n#### 二级\n```\n")
    assert out == "```text\n# 注释\n###\n#### 二级\n```"


def test_fenced_code_trailing_whitespace_kept() -> None:
    out = clean("```text\n代码   尾部空白   \n```\n")
    assert out == "```text\n代码   尾部空白   \n```"


def test_inline_code_untouched() -> None:
    """行内代码里的空格是代码的一部分,不能被中文空格修正删掉。"""
    out = clean("用 `git 中文 命令` 试一下。\n")
    assert out == "用 `git 中文 命令` 试一下。"


def test_unterminated_fence_protected_to_end() -> None:
    """未闭合的围栏一路保护到文件末尾(不把代码当正文改写)。"""
    md = "正文,句号结尾。\n\n```text\n123\n原文   空格\n"
    assert clean(md) == md


def test_fence_content_never_triggers_reports() -> None:
    """代码块不该产生「诗行/页眉/OCR 空格」之类的清理报告。"""
    from markdown.cleaner import CleanReport

    report = CleanReport()
    clean_markdown(HAZARDOUS_FENCE + "\n", report=report)
    assert report.issues == []


def test_input_with_nul_byte_is_not_fatal() -> None:
    """输入文件自带 NUL 字节时不该让清理失败(占位符检查只看占位符形状)。"""
    out = clean("正\x00文,句号结尾。\n")
    assert "\x00" in out


# ---------------------------------------------------------------- 页码注释(跨页)

def test_paragraph_joined_across_page_marker() -> None:
    """被页边界断开的正文必须拼回去 —— 页码注释不是段落边界。"""
    md = ("<!-- page 12 -->\n正文前半句一直写到了这一行的末尾为止,还没写完\n\n"
          "<!-- page 13 -->\n后半句在下一页接上,并以句号结尾。\n")
    out = clean(md)
    assert "正文前半句一直写到了这一行的末尾为止,还没写完后半句在下一页接上,并以句号结尾。" in out


def test_page_markers_preserved() -> None:
    """注释本身一条不少(内容对照要用它算页覆盖)。"""
    md = ("<!-- page 12 -->\n正文前半句\n\n<!-- page 13 -->\n后半句接上。\n\n"
          "<!-- page 14 -->\n下一段,句号结尾。\n")
    out = clean(md)
    assert out.count("<!-- page") == 3


def test_poem_not_joined_across_page_marker() -> None:
    """诗跨页仍然不拼接(诗行判据在注释两侧同样生效)。"""
    md = "<!-- page 20 -->\n床前明月光\n疑是地上霜\n\n<!-- page 21 -->\n举头望明月\n低头思故乡\n"
    out = clean(md)
    assert "床前明月光  \n疑是地上霜" in out
    assert "举头望明月  \n低头思故乡" in out


def test_verse_run_across_marker_without_blank_line() -> None:
    """注释直接夹在诗行之间时也透明:该补的硬换行不能少。"""
    out = clean("床前明月光\n<!-- page 9 -->\n疑是地上霜\n")
    assert out == "床前明月光  \n<!-- page 9 -->\n疑是地上霜"


@pytest.mark.parametrize("block, tail", [
    ("# 第三章", "# 第三章"),
    ("- 列表项一", "- 列表项一"),
    ("> 引用一行", "> 引用一行"),
    ("| A | B |\n| --- | --- |\n| 1 | 2 |", "| A | B |\n| --- | --- |\n| 1 | 2 |"),
    ("```py\nx = 1\n```", "```py\nx = 1\n```"),
])
def test_block_structure_not_joined_across_marker(block: str, tail: str) -> None:
    """标题/列表/引用/表格/代码块是块结构,跨页边界也不与上一行拼接。"""
    md = f"上一页的正文结尾没有标点\n\n<!-- page 31 -->\n{block}\n"
    out = clean(md)
    assert tail in out
    assert "结尾没有标点" + tail.split("\n")[0] not in out


# ---------------------------------------------------------------- 有序列表标记

def test_ordered_list_items_not_joined() -> None:
    """`3. ` 起的条目也是列表项 —— 曾经只认 `1. ` `2. `,第 3 条起会被拼成一行。"""
    md = "# 目录\n\n1. 第一项/3\n\n2. 第二项/8\n\n3. 第三项/27\n4. 第四项/30\n\nIV\n\n5. 第五项/35\n"
    assert clean(md) == md.rstrip("\n")


def test_ordered_list_parenthesis_style() -> None:
    """`12) ` 也是列表标记(数字 + 右括号)。"""
    out = clean("正文上一行没有标点\n\n12) 第十二项\n")
    assert out.startswith("正文上一行没有标点\n\n12)")
    assert "标点12)" not in out


def test_decimal_number_is_not_a_list_marker() -> None:
    """`3.14` 不是列表标记(数字后必须有空白)。"""
    out = clean("正文上一行没有标点\n\n3.14\n")
    assert "标点\n\n3.14" in out


# ---------------------------------------------------------------- 诗行边界

def _line(n: int) -> str:
    return "一" * n


def test_verse_threshold_is_verse_max_chars() -> None:
    """两行都在 VERSE_MAX_CHARS 以内 → 诗行(不拼接 + 硬换行);排满一行 → 正常拼接。

    「不拼接」与「补硬换行」必须用同一个门槛:两者错开时,13-18 字的诗行
    会既不拼接、又拿不到硬换行,在阅读器里照样挤成一行。

    注意 `VERSE_MAX_CHARS`(18)与 `JOIN_MIN_CHARS`(20)之间有一条窄缝(19 字):
    这种长度既不算诗行、也够不着「排满一行」,按保守处理**不拼接**(宁可少拼)。
    """
    from markdown.cleaner import JOIN_MIN_CHARS, VERSE_MAX_CHARS

    inside = clean(f"{_line(VERSE_MAX_CHARS)}\n{_line(VERSE_MAX_CHARS)}\n")
    assert inside == f"{_line(VERSE_MAX_CHARS)}  \n{_line(VERSE_MAX_CHARS)}"

    outside = clean(f"{_line(JOIN_MIN_CHARS + 1)}\n{_line(JOIN_MIN_CHARS + 1)}\n")
    assert outside == _line(JOIN_MIN_CHARS + 1) + _line(JOIN_MIN_CHARS + 1)

    gap = clean(f"{_line(VERSE_MAX_CHARS + 1)}\n{_line(VERSE_MAX_CHARS + 1)}\n")
    assert gap == f"{_line(VERSE_MAX_CHARS + 1)}\n{_line(VERSE_MAX_CHARS + 1)}"


def test_body_line_is_joined_to_its_continuation() -> None:
    """排满一行的正文断行照旧拼接(这是 join_lines 存在的理由)。"""
    out = clean("他走进屋子看了看四周,桌上放着一封没有署名的信\n纸上只有一句话:明天中午老地方见。\n")
    assert out == "他走进屋子看了看四周,桌上放着一封没有署名的信纸上只有一句话:明天中午老地方见。"


def test_short_line_with_end_punctuation_is_not_joined() -> None:
    """短行(不足排满一行)不再被当作被断开的正文 —— 它更像版式行(标题/字段)。

    这是本次专项收紧的取舍:真实扫描书里被误拼的正是 11-19 字的短行。
    """
    out = clean("他走进屋子看看\n桌上放着一封信。\n")
    assert out == "他走进屋子看看\n桌上放着一封信。"


def test_two_short_lines_without_punctuation_treated_as_verse() -> None:
    """已知边界(保守取舍):两行都短且都没有句末标点 → 当诗行处理。

    代价是这种形状的散文断行不会被拼成一段(而是保留分行)。反向代价更大 ——
    七言诗每行 7 字,只要按长度拼就必然把整首诗拼成一行,所以这里选「保留分行」。
    真实书籍里排满的正文行普遍 20 字以上,与诗行之间有很宽的间隔。
    """
    out = clean("他走进屋子看看\n桌上放着一封信\n")
    assert out == "他走进屋子看看  \n桌上放着一封信"


# ---------------------------------------------------------------- 页码边界

def test_page_number_rule_keeps_years_and_long_numbers() -> None:
    """1-3 位独立数字行当页码删;4 位(年份)与带小数点的行保留。"""
    out = clean("正文一,句号结尾。\n\n123\n\n2024\n\n正文二,句号结尾。\n\n3.14\n")
    assert "\n123\n" not in out
    assert "2024" in out
    assert "3.14" in out


# ---------------------------------------------------------------- 脚注保护

#: 正文行**不以句末标点结尾** + 脚注定义紧跟其后 —— OCR 产物最常见的形态,
#: 也是修复前必然被 `join_lines` 拼进正文的形状(定义语法就此消失)。
FOOTNOTE_HAZARD = ("这种方法的效果在实验中得到了验证,而且重复了三次以上[^1]\n\n"
                   "[^1]: 第一条脚注,内容是中文,长度足够长\n"
                   "[^2]: 第二条脚注,内容也是中文,同样足够长\n\n"
                   "结论段落正文正文正文正文正文[^2],以句号结尾。\n")


def test_footnote_definitions_not_joined_into_body() -> None:
    """脚注定义是块结构,不得被拼进上一段(修复前定义会整条消失)。"""
    out = clean(FOOTNOTE_HAZARD)
    assert "[^1]: 第一条脚注,内容是中文,长度足够长" in out
    assert "[^2]: 第二条脚注,内容也是中文,同样足够长" in out
    assert "三次以上[^1][^1]:" not in out
    assert "长度足够长[^2]:" not in out
    assert out.count("[^") == 4          # 2 处引用 + 2 条定义


@pytest.mark.parametrize("md", [
    "正文正文正文正文正文[^1]\n\n[^1]: 脚注内容\n",                        # 单个
    "正文正文正文正文正文[^1]正文正文正文正文[^2]\n\n[^1]: 第一条脚注\n[^2]: 第二条脚注\n",  # 多个
    "正文正文正文正文正文[^1]\n\n[^1]: 第一行\n    第二行\n    第三行\n",   # 跨多行
    "正文正文正文正文正文[^1]\n\n[^1]: 这是 *强调*,以及 `代码`。\n",        # 脚注内含 Markdown
    "正文正文正文正文正文[^1]\n\n[^1]: 中文标点,。!?;:【注意】\n",          # 中文标点
])
def test_footnote_cases_survive_cleaning(md: str) -> None:
    """五类脚注样本:清理前后引用与定义都在,且定义块逐字节不变。"""
    out = clean(md)
    assert out == md.rstrip("\n")


def test_footnote_multiline_indent_kept() -> None:
    """跨行脚注的续行缩进必须保留(pandoc 靠缩进判定脚注内容;strip 掉就散架)。"""
    out = clean("正文正文正文正文正文[^1]\n\n[^1]: 第一行\n    第二行\n    第三行\n")
    assert "[^1]: 第一行\n    第二行\n    第三行" in out
    assert "第一行  \n" not in out      # 不得被诗行规则补上硬换行


def test_footnote_content_is_not_rewritten() -> None:
    """脚注块整体不改写:与代码块同一取舍(宁可留空格噪声,不改语义)。

    规则链跑在掩码文本上,所以中文空格修正等规则不会打进脚注 —— 脚注里
    `Py Mu PDF` 之类的 OCR 空格会留着,这是刻意的保守选择。
    """
    md = "正文正文正文正文正文[^1]\n\n[^1]: 脚注 里 的 空格 与 Py Mu PDF 保持原样\n"
    assert clean(md) == md.rstrip("\n")


def test_inline_footnote_ref_untouched() -> None:
    """行内的 `[^1]` 引用不是块结构,正文照常清理。"""
    out = clean("正文 中文 之间 有空格[^1]。\n")
    assert out == "正文中文之间有空格[^1]。"


def test_footnote_syntax_inside_code_fence_untouched() -> None:
    """代码块里的 `[^1]: …` 是示例文本,既不当脚注也不被改写。"""
    md = "```text\n[^1]: 代码里的示例\n    缩进续行\n```\n\n正文正文正文正文正文[^1]。\n"
    assert clean(md) == md.rstrip("\n")


def test_footnote_syntax_inside_inline_code_untouched() -> None:
    """行内代码里的脚注语法不参与掩码(否则还原顺序会留下未还原的占位符)。"""
    md = "正文正文正文正文正文[^1]。\n\n[^1]: 见 `x = 1` 与 `[^2]: 不是脚注` 的说明。\n"
    assert clean(md) == md.rstrip("\n")


def test_underindented_footnote_continuation_is_normal_text() -> None:
    """缩进不足 4 空格的续行 pandoc 本来就不认作脚注内容,按普通文本处理。"""
    out = clean("正文正文正文正文正文[^1]。\n\n[^1]: 第一行\n  第二行没有足够缩进\n")
    assert out.startswith("正文正文正文正文正文[^1]。\n\n[^1]: 第一行\n")


# ---------------------------------------------------------------- 反误伤:刻意换行与结构行

def test_unpunctuated_line_breaks_are_kept() -> None:
    """没有句末标点 ≠ 断行:刻意换行的短行不得被自动合并(诗歌/自由分行/逐行内容)。

    修复前这类行会因「上一行没有句末标点」被拼成一行;现在只加硬换行保住分行。
    """
    cases = [
        ("春风吹过山岗\n河水流向远方\n", "春风吹过山岗  \n河水流向远方"),        # 两行都短
        ("这是第一行\n这是第二行\n这是第三行\n", "这是第一行  \n这是第二行  \n这是第三行"),
        ("第一项\n第二项\n第三项\n", "第一项  \n第二项  \n第三项"),
        ("两个黄鹂鸣翠柳\n一行白鹭上青天\n", "两个黄鹂鸣翠柳  \n一行白鹭上青天"),  # 七言
    ]
    for md, expected in cases:
        assert clean(md) == expected, md


def test_catalog_entries_are_not_merged() -> None:
    """目录条目是独立条目:长度超过诗行门槛(18 字)也不得被拼成一行。

    取自真实书籍目录形态(`一 历史的回顾 …… 1`);修复前三条会粘成
    `第一章…/1第二章…/15第三章…/32`,在阅读器里整页目录变成一行。
    """
    md = ("一 历史的回顾 …… 1\n二 反对英国的殖民统治 …… 8\n三 武装斗争 …… 32\n")
    out = clean(md)
    assert out.startswith("一 历史的回顾 …… 1")
    assert "\n二 反对英国的殖民统治 …… 8" in out
    assert "\n三 武装斗争 …… 32" in out
    assert "1二 反对" not in out


def test_catalog_entry_does_not_swallow_previous_body_line() -> None:
    """正文行(无句末标点)后面紧跟目录条目时,条目不得被吞进正文。"""
    md = "这一段正文被断开了,前半句写到这里\n\n一 历史的回顾 …… 1\n"
    out = clean(md)
    assert "\n\n一 历史的回顾 …… 1" in out
    assert "写到这里一 历史的回顾" not in out


def test_note_lines_are_not_merged_into_the_next_paragraph() -> None:
    """注释行(圈码序号开头)是独立段落:没有句末标点时也不得被下一段粘走。"""
    md = "① 参见前揭书第三二页\n\n这一段正文足够长,会被跨空行拼接规则看上。\n"
    out = clean(md)
    assert out.startswith("① 参见前揭书第三二页")
    assert "第三二页这一段" not in out


def test_note_lines_are_not_treated_as_running_heads() -> None:
    """重复出现的注释行(`① 同上`)是有内容的行,不是页眉页脚装饰。"""
    md = "\n\n".join(["① 同上"] * 4 + ["正文段落,以句号结尾。"])
    out = clean(md)
    assert out.count("① 同上") == 4


def test_note_block_keeps_one_note_per_line() -> None:
    """连续排布的注释(`① …`/`② …`)保持一行一条(靠硬换行,而不是被拼成一段)。"""
    md = ("① 列宁:《告犹太工人书》,《列宁全集》第八卷第四六三页。\n"
          "② 列宁:《崩得在党内的地位》,《列宁全集》第七卷第八四页。\n")
    out = clean(md)
    assert "第四六三页。  \n②" in out


def test_heading_keeps_internal_spaces() -> None:
    """标题行里的空格是序号与标题的分隔,不得被中文空格修正删掉。"""
    assert clean("# 第一章 测试\n") == "# 第一章 测试"
    assert clean("## 一 历史的回顾\n") == "# 一 历史的回顾"


def test_layout_lines_keep_field_spaces() -> None:
    """独立成段的版式行(版权页字段)里的空格是字段分隔,不得删除。

    真实扫描书实测:全篇只有版权页这几行的空格被删 —— 删完
    `人民出版社出版 新華書店发行` 就粘成一个词。
    """
    md = ("人民出版社出版 新華書店发行\n\n"
          "787×1092毫米32开本 2.25印张 43.000字\n\n"
          "书号 3001·1505 定价 0.15 元\n")
    assert clean(md) == md.rstrip("\n")


def test_body_lines_still_get_space_cleanup() -> None:
    """反例的另一半:正文行照旧做空格修正(长行、或以标点收尾的行)。"""
    assert clean("这是 中文 排版 空格修正的例子。\n") == "这是中文排版空格修正的例子。"
    assert clean("这是一行足够长的中文正文,里面 插了 位置奇怪的 空格。\n") \
        == "这是一行足够长的中文正文,里面插了位置奇怪的空格。"


def test_real_page_break_is_still_joined() -> None:
    """反误伤不能把主功能一起收掉:排满一行的真正跨页断行仍要拼接。"""
    md = "上一段在这里被断开了,前半句一直写到了这一行末尾为止\n\n下一行继续,并以句号结尾。\n"
    assert clean(md) == "上一段在这里被断开了,前半句一直写到了这一行末尾为止下一行继续,并以句号结尾。"


# ---------------------------------------------------------------- 行间数学块

def test_math_block_is_protected_verbatim() -> None:
    """`$$` 定界行与块内容不得被改写(不加硬换行、不被拼走)。

    修复前 `$$` 会被 `_is_verse_line` 当成 2 字符短行(长度 ≤ VERSE_MAX_CHARS 且无句末
    标点)而补上行尾硬换行 —— 但 pandoc 不会把数学块里的行尾空格渲染成 `<br>`,内容
    校验据此给**正常产物**报「硬换行减少」的假警告。单行块与 `\\[ … \\]` 同理。
    """
    block = "$$\na^2+b^2=c^2\n$$\n"
    assert clean(block) == block.rstrip("\n")

    single = "$$E = mc^2$$\n"
    assert clean(single) == single.rstrip("\n")

    bracket = "\\[\nE = mc^2\n\\]\n"
    assert clean(bracket) == bracket.rstrip("\n")


def test_math_block_is_not_joined_with_following_paragraph() -> None:
    """多行数学块不得被压平,也不得与紧随其后的正文粘成同一段(幂等)。

    修复前 `_join_broken_lines` 会把 `\\begin{aligned}` 这类 ≥20 字的行一路拼下去,
    产物里 `<math display="block">…</math>` 与后一句正文进了同一个 `<p>`。
    """
    md = r"""$$
\begin{aligned}
(a+b)^2 &= a^2 + 2ab + b^2 \\
(a-b)^2 &= a^2 - 2ab + b^2
\end{aligned}
$$

这里是正文,应当与公式分开成段。
"""
    out = clean(md)
    assert out == md.rstrip("\n")
    assert out.count("\n") == md.rstrip("\n").count("\n")   # 行数不变:没有被压平
    assert "\n\n这里是正文" in out                           # 公式与正文仍是两个结构
    assert "\\end{aligned}$$这里是正文" not in out
    assert clean(out) == out                               # 幂等(硬换行不会叠加)


def test_text_around_math_block_is_still_cleaned() -> None:
    """掩码只覆盖数学块本身:块前后的正文照旧走空格/断行清理。"""
    md = "$$\nE = mc^2\n$$\n\n前面一段 中文 空格 要修正。\n"
    out = clean(md)
    assert "$$\nE = mc^2\n$$" in out
    assert "前面一段中文空格要修正。" in out


def test_unclosed_math_delimiter_is_left_alone() -> None:
    """孤立 `$$`(找不到配对收尾行)不掩码 —— 否则后面整本书会静默失去清理。

    这是「不该改」的反例:宁可漏保护一处数学,也不能让清理器形同失效。
    两种形态都要覆盖:定界符在行中、定界符在行首但全篇没有收尾行。
    """
    inline = "价格写作 $$ 符号,这句话只是字面出现的美元符号。\n\n这一段 中文 空格 照旧要修正。\n"
    out = clean(inline)
    assert "$$ 符号" in out
    assert "这一段中文空格照旧要修正。" in out          # 后面的正文照旧被清理

    orphan = "$$ dollar\n\n另一段 中文 空格 也要修正。\n"
    out2 = clean(orphan)
    assert out2.startswith("$$ dollar")                 # 孤立定界行原样保留
    assert "另一段中文空格也要修正。" in out2


# ---------------------------------------------------------------- 开关与配置

def test_clean_keys_cover_all_option_fields() -> None:
    fields = {f for f in CleanOptions.__dataclass_fields__}
    assert set(CLEAN_KEYS) == fields


def test_disable_by_name() -> None:
    opts = resolve_options({"clean": {"running_heads": True}}, "running_heads")
    assert opts.running_heads is False


def test_unknown_clean_key_raises() -> None:
    with pytest.raises(ValueError):
        normalize_clean_keys("no_such_rule")


def test_options_from_config() -> None:
    opts = CleanOptions.from_config({"clean": {"cjk_spaces": False, "bold": True}})
    assert opts.cjk_spaces is False
    assert opts.bold is True
    assert opts.running_heads is True   # 未配置项取默认值
