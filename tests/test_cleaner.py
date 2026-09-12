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
