"""IR → Markdown(阶段 B §E.5 不变量 1:往返逐字节一致)。

渲染规则只有一条:**按块序把 ``text`` 与 ``extra['trailer']`` 原样拼回去**。
没有任何"顺手补空行""统一标题层级"之类的整理 —— 那些属于清理规则(P2/P3),
必须经过 Patch 并落进报告,不能在渲染层偷偷发生。

因此这里的输出与输入相等的条件是:块未被改写。一旦 ``BookIR.rewrite`` 改过正文,
``assert_roundtrip`` 会如实地指出第一个不同的位置,而不是假装没事。
"""
from __future__ import annotations

from typing import Iterable, Sequence

from ir.model import Block, BookIR, IRError


class RoundtripError(IRError):
    """往返不一致(有内容被丢弃或重复拼接)。"""

    def __init__(self, position: int, expected: str, actual: str, *, source: str = "") -> None:
        where = f"'{source}' " if source else ""
        super().__init__(
            f"{where}往返在第 {position} 个字符处分叉:"
            f"原文 {expected!r} ≠ 渲染 {actual!r}"
        )
        self.position = position
        self.expected = expected
        self.actual = actual


def render_blocks(blocks: Iterable[Block]) -> str:
    """按给定顺序把块拼回文本(``text`` + ``trailer``)。"""
    return "".join(b.text + str(b.extra.get("trailer", "")) for b in blocks)


def render_ir(ir: BookIR) -> str:
    """整本渲染:文首空白 + 所有**可编辑**块(证据块不参与渲染)。"""
    editable = sorted(ir.editable_blocks, key=lambda b: b.order)
    return ir.meta.prefix + render_blocks(editable)


def first_divergence(expected: str, actual: str) -> int | None:
    """第一个不同的字符位置;完全相同返回 ``None``。"""
    limit = min(len(expected), len(actual))
    for i in range(limit):
        if expected[i] != actual[i]:
            return i
    return None if len(expected) == len(actual) else limit


def assert_roundtrip(ir: BookIR, md: str, *, source: str = "") -> None:
    """断言 ``render_ir(ir) == md``;不一致时指出位置与两侧内容。"""
    rendered = render_ir(ir)
    pos = first_divergence(md, rendered)
    if pos is None:
        return
    left = md[pos: pos + 30]
    right = rendered[pos: pos + 30]
    raise RoundtripError(pos, left, right, source=source)


def roundtrip_ok(ir: BookIR, md: str) -> bool:
    return render_ir(ir) == md


def spans_tile(ir: BookIR, md: str) -> tuple[bool, str]:
    """切片是否"无遗漏、无重复"地铺满全文(§E.5 不变量 2)。

    判据(逐条对应一种真实事故):

    1. 首块起点 == ``len(prefix)``、末块 ``end + trailer`` == ``len(md)``(空洞/截断);
    2. 相邻块 ``start[i+1] == end[i] + len(trailer[i])``(重叠或跳字);
    3. ``md[start:end] == text``(解析期偷偷规整过正文);
    4. 每块 span 互不重叠(重复切分)。
    """
    blocks = sorted(ir.editable_blocks, key=lambda b: b.order)
    if not blocks:
        return (md == ir.meta.prefix), "没有可编辑块"
    cursor = len(ir.meta.prefix)
    for i, block in enumerate(blocks):
        start, end = block.md_span or (-1, -1)
        if start != cursor:
            return False, f"块 {block.id} 起点 {start} ≠ 期望 {cursor}(空洞或重叠)"
        if md[start:end] != block.text:
            return False, f"块 {block.id} 的 text 与 md[{start}:{end}] 不一致"
        cursor = end + len(str(block.extra.get("trailer", "")))
    if cursor != len(md):
        return False, f"末块结束于 {cursor},全文长 {len(md)}(尾部丢失)"
    return True, ""


def editable_runs(ir: BookIR) -> Sequence[tuple[int, int]]:
    """所有可编辑块的 ``(start, end)``,按起点排序(越界/空块自检用)。"""
    return tuple(sorted((b.md_span or (0, 0)) for b in ir.editable_blocks))
