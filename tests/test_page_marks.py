"""页注释:生成、识别,以及给**旧缓存**补页注释。

页注释(`<!-- page N -->`)是这条链路里唯一能表达「这页在哪」的东西:内容对照的
页覆盖、IR 的页归属、人工按页定位都靠它。PaddleOCR 在 v0.6.2 之前不写它,于是
那两本书的页覆盖校验一直是 0(不是 0%,是「没法算」);旧缓存又不能靠重新 OCR
补,只能用 `content_list` 里 `page_break` 块的 offset 原地补。
"""
from __future__ import annotations

import re

from pdf2epub.markdown.footnotes import PAGE_BREAK_TYPE
from pdf2epub.page_result import (
    PAGE_BREAK_BLOCK_TYPE,
    ensure_page_marks,
    page_marker,
    page_marks,
)


def test_page_break_type_matches_the_footnote_engine() -> None:
    """两处各自定义同一个字符串:不一致时页边界块会被悄悄漏掉,不会报错。"""
    assert PAGE_BREAK_BLOCK_TYPE == PAGE_BREAK_TYPE


def test_page_marker_round_trips() -> None:
    md = f"{page_marker([1])}\n\n正文一\n\n{page_marker([2])}\n\n正文二"
    assert page_marks(md) == [[1], [2]]


def test_ensure_page_marks_inserts_and_shifts_offsets() -> None:
    """旧缓存(无页注释)→ 在每个页边界插入页注释,并把 offset 平移到同一位置。"""
    first, second = "第一页正文,句号结尾。", "第二页正文,也以句号结尾。"
    md = f"{first}\n\n{second}"
    blocks = [
        {"type": "page_break", "page_idx": 0, "offset": 0},
        {"type": "text", "text": first, "page_idx": 0},
        {"type": "page_break", "page_idx": 1, "offset": len(first) + 2},
        {"type": "text", "text": second, "page_idx": 1},
    ]

    new_md, shifted = ensure_page_marks(md, blocks)

    assert page_marks(new_md) == [[1], [2]]
    # 块 offset 仍指向**该页内容的起点**(页注释就在它前面)
    assert new_md[shifted[0]["offset"]:].startswith("<!-- page 1 -->")
    assert new_md[shifted[2]["offset"]:].startswith("<!-- page 2 -->")
    # 正文一个字节没变(只插入注释;插入点会留下多余空行,由清理层的空行压缩收敛)
    stripped = re.sub(r"<!-- page \d+ -->", "", new_md)
    assert re.sub(r"\n{3,}", "\n\n", stripped).strip() == md


def test_ensure_page_marks_is_idempotent() -> None:
    md = f"{page_marker([1])}\n\n正文"
    blocks = [{"type": "page_break", "page_idx": 0, "offset": 0}]
    new_md, new_blocks = ensure_page_marks(md, blocks)
    assert new_md == md and new_blocks is blocks


def test_ensure_page_marks_without_page_breaks_is_noop() -> None:
    """拿不到页边界就不猜:原样返回,不写半套注释。"""
    md = "正文一段,没有页信息。"
    blocks = [{"type": "text", "text": md, "page_idx": 0}]
    assert ensure_page_marks(md, blocks) == (md, blocks)


def test_ensure_page_marks_ignores_out_of_range_offsets() -> None:
    md = "短"
    blocks = [{"type": "page_break", "page_idx": 0, "offset": 999}]
    assert ensure_page_marks(md, blocks) == (md, blocks)


def test_page_marker_covers_multiple_pages() -> None:
    assert page_marker([4, 5]) == "<!-- page 4-5 -->"
    assert page_marker([4, 6]) == "<!-- page 4,6 -->"
