"""MinerU 结构化结果 → 页脚脚注补回(离线单测)。

背景(真实云端实测):MinerU 的 markdown 渲染只走 `para_blocks`,而 `page_footnote`
属于 **discarded blocks** —— 同一份结果包里,脚注文本只出现在 `content_list.json`,
`full.md` 里一个字都没有。补回逻辑因此只能按「结构化结果 + 阅读顺序对齐」来做。

这里被测的是纯函数(`recover_page_footnotes` / `load_content_list` /
`content_list_text`),不发任何网络请求;整条链路(解包 → 落盘 → 合并)的验证在
`test_ocr_sharding_resume.py` 里用假云端跑。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backends.mineru_backend import (content_list_text, load_content_list,
                                     recover_page_footnotes)

MD = ("# 第一章 测试章节\n\n"
      "本页用于验证云端 OCR 是否保留脚注。正文里出现一个注释标记①,\n\n"
      "随后继续写作,补充足够长度的文字以便版面分析能正确切块。\n\n"
      "第二段正文继续说明:脚注是学术书籍中常见的排版元素。\n")

BLOCKS_V1 = [
    {"type": "text", "text": "第一章 测试章节", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "本页用于验证云端 OCR 是否保留脚注。正文里出现一个注释标记①,",
     "page_idx": 0},
    {"type": "text", "text": "第二段正文继续说明:脚注是学术书籍中常见的排版元素。",
     "page_idx": 0},
    {"type": "page_footnote", "text": "① 这是测试脚注:内容不能丢。", "page_idx": 0},
    {"type": "page_number", "text": "1", "page_idx": 0},
]


def test_page_footnote_inserted_after_its_pages_last_block() -> None:
    """脚注补在该页最后一块内容之后(不是文末),页码/页眉这类装饰不补。"""
    out, count = recover_page_footnotes(MD, BLOCKS_V1)

    assert count == 1
    assert out.startswith(MD.rstrip("\n"))
    assert out.rstrip("\n").endswith("① 这是测试脚注:内容不能丢。")
    assert "\n1\n" not in out


def test_crlf_artifacts_are_handled() -> None:
    """云端 `full.md` 常见 CRLF:锚点必须照样能对齐(只找 `\\n\\n` 会全落空 → 兜到文末)。"""
    md = MD.replace("\n", "\r\n")
    blocks = [
        {"type": "text", "text": "本页用于验证云端 OCR 是否保留脚注。正文里出现一个注释标记①,",
         "page_idx": 0},
        {"type": "page_footnote", "text": "① CRLF 也要补在这一段之后。", "page_idx": 0},
    ]

    out, count = recover_page_footnotes(md, blocks)

    assert count == 1
    assert out.index("① CRLF 也要补在这一段之后。") < out.index("第二段正文继续说明")


def test_v2_grouped_blocks_are_supported() -> None:
    """`content_list_v2.json` 是按页分组且内容嵌在 `content.*_content[]` 里的。"""
    v2 = [[
        {"type": "title", "content": {"title_content": [{"type": "text", "content": "第一章 测试章节"}],
                                      "level": 1},
         "bbox": [1, 2, 3, 4]},
        {"type": "paragraph",
         "content": {"paragraph_content": [
             {"type": "text", "content": "第二段正文继续说明:脚注是学术书籍中常见的排版元素。"}]}},
        {"type": "page_footnote",
         "content": {"page_footnote_content": [{"type": "text", "content": "① 这是测试脚注:内容不能丢。"}]}},
    ]]

    out, count = recover_page_footnotes(MD, v2[0])

    assert count == 1
    assert "这是测试脚注" in out


def test_unanchored_footnote_falls_back_to_the_end() -> None:
    """正文对不上(OCR 文本差异大)时兜底插到文末 —— 位置可以不精确,内容不能丢。"""
    blocks = [
        {"type": "text", "text": "完全对不上的另一段文字。", "page_idx": 0},
        {"type": "page_footnote", "text": "① 掉落位置不精确的脚注。", "page_idx": 0},
    ]

    out, count = recover_page_footnotes(MD, blocks)

    assert count == 1
    assert out.startswith(MD.rstrip("\n"))
    assert out.rstrip("\n").endswith("① 掉落位置不精确的脚注。")


def test_no_footnote_blocks_leaves_markdown_untouched() -> None:
    """没有 page_footnote(或结构化结果为空)→ 原文一字不动,计数 0。"""
    assert recover_page_footnotes(MD, [b for b in BLOCKS_V1 if b["type"] != "page_footnote"]) \
        == (MD, 0)
    assert recover_page_footnotes(MD, []) == (MD, 0)
    assert recover_page_footnotes("", BLOCKS_V1) == ("", 0)


def test_multiple_footnotes_keep_reading_order() -> None:
    """同一页多条脚注按阅读顺序补回(逆序插入不能让它们反过来)。"""
    blocks = BLOCKS_V1[:3] + [
        {"type": "page_footnote", "text": "① 第一条。", "page_idx": 0},
        {"type": "page_footnote", "text": "② 第二条。", "page_idx": 0},
    ]

    out, count = recover_page_footnotes(MD, blocks)

    assert count == 2
    assert out.index("① 第一条。") < out.index("② 第二条。")


# ---------------------------------------------------------------- 读取容错

def test_load_content_list_accepts_v1_and_v2(tmp_path: Path) -> None:
    v1 = tmp_path / "a_content_list.json"
    v1.write_text(json.dumps(BLOCKS_V1, ensure_ascii=False), encoding="utf-8")
    v2 = tmp_path / "b_content_list_v2.json"
    v2.write_text(json.dumps([[{"type": "paragraph",
                                "content": {"paragraph_content": [
                                    {"type": "text", "content": "一段话。"}]}}]]),
                  encoding="utf-8")

    assert [b["type"] for b in load_content_list(v1)][:2] == ["text", "text"]
    assert load_content_list(v2)[0]["type"] == "paragraph"


@pytest.mark.parametrize("content", ["{ 不是 JSON", "{}", "[]"])
def test_load_content_list_tolerates_broken_or_empty(content: str, tmp_path: Path) -> None:
    """坏了/为空就当没有结构化结果(不能因为一个附带文件让整本书转换失败)。"""
    path = tmp_path / "content_list.json"
    path.write_text(content, encoding="utf-8")

    assert load_content_list(path) == []
    assert load_content_list(tmp_path / "缺失.json") == []


def test_content_list_text_reads_both_shapes() -> None:
    assert content_list_text({"type": "text", "text": " 纯文本 "}) == "纯文本"
    assert content_list_text({"type": "paragraph", "content": {
        "paragraph_content": [{"type": "text", "content": "第一行"},
                              {"type": "text", "content": "第二行"}]}}) == "第一行第二行"
    assert content_list_text({"type": "image", "img_path": "images/x.jpg"}) == ""
