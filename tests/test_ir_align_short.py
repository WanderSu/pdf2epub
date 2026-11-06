"""短块(不足锚点门槛)的页归属:只在**唯一**命中证据时才认。

背景:``block_anchor`` 对正文块要求归一化后 ≥12 字才生成锚点,短于此的整块不参与
匹配 —— 跨页被切开的续行(「心,我这里有。”」这类 3–10 字)因此永远拿不到页号,
跨块拼接只能停在建议。这里用合成夹具固定新行为:

  - 左右两个已匹配块之间、窗口内**唯一**同文的短块 → 归还页号(``how="window"``);
  - 窗口内出现两次同文 → 放弃(不猜);
  - 短块不从窗口外取证(防止页号乱跳);
  - 链接仍保持 ``(block.order, evidence.order)`` 单调。
"""
from __future__ import annotations

from ir import align_blocks, build_ir, links_monotonic

LONG_A = "第一段正文,内容足够长,能被锚点匹配上。"
LONG_B = "第二段正文,内容同样足够长,也能被锚点匹配上。"
LONG_C = "第三段正文,内容依然足够长,还可以被锚点匹配上。"

MD = f"""\
{LONG_A}

野心

{LONG_B}

我说:

{LONG_C}
"""


def _evidence(*rows):
    return [
        {"type": "text", "text": text, "page_idx": page, "bbox": [10, 20, 300, 40]}
        for text, page in rows
    ]


def _blocks(ir):
    return {b.text: b for b in ir.editable_blocks}


def test_short_block_between_two_matches_gets_page() -> None:
    ir = build_ir(MD, source="short.md", backend="mineru", total_pages=4,
                  evidence=_evidence(
                      (LONG_A, 0),
                      ("野心", 0),
                      (LONG_B, 1),
                      ("我说:", 1),
                      (LONG_C, 2),
                  ))
    blocks = _blocks(ir)
    assert blocks["野心"].page_idx == 0
    assert blocks["我说:"].page_idx == 1
    assert links_monotonic(ir.links, ir.blocks)
    hows = {l.block_id: l.how for l in ir.links}
    assert hows[blocks["野心"].id] == "window"


def test_duplicate_short_text_in_window_is_not_guessed() -> None:
    """同一短句在窗口里出现两次 → 不认(猜错页号会污染整本页账)。"""
    ir = build_ir(MD, source="short.md", backend="mineru", total_pages=4,
                  evidence=_evidence(
                      (LONG_A, 0),
                      ("野心", 0),
                      ("野心", 0),
                      (LONG_B, 1),
                      (LONG_C, 2),
                  ))
    assert _blocks(ir)["野心"].page_idx is None


def test_short_block_does_not_reach_outside_its_window() -> None:
    """窗口外同文不算:证据里只在很靠后的位置出现 → 与阅读顺序矛盾,放弃。"""
    ir = build_ir(MD, source="short.md", backend="mineru", total_pages=4,
                  evidence=_evidence(
                      (LONG_A, 0),
                      (LONG_B, 1),
                      (LONG_C, 2),
                      ("野心", 3),
                  ))
    assert _blocks(ir)["野心"].page_idx is None


def test_one_char_block_never_matched() -> None:
    """1 个字没有判别力:不参与窗口匹配。"""
    md = f"{LONG_A}\n\n我\n\n{LONG_B}\n"
    ir = build_ir(md, source="short.md", backend="mineru", total_pages=4,
                  evidence=_evidence((LONG_A, 0), ("我", 0), (LONG_B, 1)))
    assert _blocks(ir)["我"].page_idx is None
