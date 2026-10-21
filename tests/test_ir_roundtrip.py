"""IR 往返与切片完整性(阶段 B §E.5 不变量 1、2)。

这一组测试是 P1 的验收闸门:

  - ``render(parse(book.md))`` 与 ``book.md`` **逐字节相同**(关闭所有规则、不写页注释)
  - 可编辑块的 ``md_span`` 首尾相接(无空洞、无重叠、无重复)
  - ``blocks.jsonl`` 两次运行字节相同(差分测试的前提)
  - 证据块只读(改写请求被拒,正文不变)

夹具是**合成文本**,不含任何真实书籍正文;真实缓存书只在本地作为额外回归
(存在则跑,不存在则跳过 —— 空克隆的机器上不应因此变红)。
"""
from __future__ import annotations

import json

import pytest

from conftest import PROJECT_ROOT
from ir import (IRError, ReadonlyBlockError, assert_roundtrip, build_ir,
                block_to_dict, dump_ir, ir_dir, load_ir, parse_markdown,
                render_ir, spans_tile)

#: 覆盖九类易错结构:标题层级、围栏代码、表格、列表、公式、图片、脚注定义、
#: 页码注释、硬换行(行尾两个空格)
SYNTHETIC_MD = """\
![](images/cover.png)

# 第一章 起头

正文第一段,句尾没有标点的话很容易被误判为断句。

## 1.1 小节

第二段**加粗**与 `行内代码` 混排,以及 $x^2+y^2$ 行内公式。

```python
def f(x):
    return x + 1
```

| 列一 | 列二 |
| --- | --- |
| 甲 | 乙 |

- 列表项一
- 列表项二
  - 嵌套项

$$
E = mc^2
$$

> 引用块第一行
> 引用块第二行

<!-- page 12 -->

结尾段落[^1]。

[^1]: 一条脚注的正文。
"""


def _ir(md: str = SYNTHETIC_MD, **kwargs):
    kwargs.setdefault("source", "synthetic.md")
    kwargs.setdefault("backend", "markdown")
    kwargs.setdefault("total_pages", 3)
    return build_ir(md, **kwargs)


# ---------------------------------------------------------------- 往返

def test_render_roundtrips_byte_for_byte() -> None:
    """构造出来就该相等:render 只按块序拼 text+trailer,不做任何整理。"""
    ir = _ir()
    assert render_ir(ir) == SYNTHETIC_MD
    assert_roundtrip(ir, SYNTHETIC_MD)


def test_spans_tile_the_whole_document() -> None:
    """切片完整:首尾相接、无空洞、无重叠,且每块 text 就是原文切片。"""
    ir = _ir()
    ok, why = spans_tile(ir, SYNTHETIC_MD)
    assert ok, why

    blocks = sorted(ir.editable_blocks, key=lambda b: b.order)
    cursor = len(ir.meta.prefix)
    for block in blocks:
        start, end = block.md_span
        assert start == cursor, f"{block.id} 起点 {start} != {cursor}"
        assert SYNTHETIC_MD[start:end] == block.text
        cursor = end + len(block.extra["trailer"])
    assert cursor == len(SYNTHETIC_MD)


def test_block_count_is_sane_and_kinds_are_detected() -> None:
    ir = _ir()
    kinds = [b.kind for b in ir.editable_blocks]
    assert kinds[0] == "image"
    assert kinds.count("heading") == 2
    assert "code" in kinds and "table" in kinds and "equation" in kinds
    assert "list_item" in kinds and "footnote" in kinds
    # 页码注释是"结构标记",不是正文:保留原文但类型为 unknown + role
    marker = next(b for b in ir.editable_blocks if b.extra.get("role") == "page_marker")
    assert marker.text == "<!-- page 12 -->"
    assert marker.extra["page"] == 12


def test_heading_keeps_its_marker_and_level() -> None:
    ir = _ir()
    headings = [b for b in ir.editable_blocks if b.kind == "heading"]
    assert [h.level for h in headings] == [1, 2]
    assert headings[1].text.startswith("## ")          # 标记原样保留(往返需要它)
    assert headings[0].extra["marker"] == "#"


def test_crlf_and_missing_trailing_newline_survive() -> None:
    """换行风格与原文件末尾缺换行都不能被"规整"掉。"""
    for md in ("# 标题\r\n\r\n正文一段\r\n", "第一段\n\n第二段"):
        ir = build_ir(md, source="x.md", backend="markdown")
        assert render_ir(ir) == md
        ok, why = spans_tile(ir, md)
        assert ok, why


def test_leading_blank_lines_are_preserved_in_meta_prefix() -> None:
    md = "\n\n# 标题\n\n正文\n"
    ir = build_ir(md, source="x.md", backend="markdown")
    assert ir.meta.prefix == "\n\n"
    assert render_ir(ir) == md


def test_empty_document_is_not_a_crash() -> None:
    ir = build_ir("", source="empty.md", backend="markdown")
    assert ir.editable_blocks == ()
    assert render_ir(ir) == ""


def test_rewrite_changes_only_the_target_block() -> None:
    ir = _ir()
    target = ir.editable_blocks[2]
    ir2 = ir.rewrite(target.id, "改写后的这一段。")
    assert render_ir(ir2) != SYNTHETIC_MD
    assert "改写后的这一段。" in render_ir(ir2)
    start, end = ir2.block(target.id).md_span
    assert end - start == len("改写后的这一段。")
    # 原始 IR 不受影响(不可变)
    assert render_ir(ir) == SYNTHETIC_MD


def test_unknown_block_id_is_an_error() -> None:
    with pytest.raises(IRError):
        _ir().rewrite("不存在#m0", "x")


def test_mismatched_span_is_rejected_at_construction() -> None:
    """解析期"顺手规整正文"必须当场失败,而不是产出一份对不上的 IR。"""
    block = parse_markdown("第一段\n").blocks[0]
    from dataclasses import replace

    with pytest.raises(IRError):
        replace(block, text=block.text + "偷偷加的字")


# ---------------------------------------------------------------- 证据只读

def _evidence_items():
    return [
        {"type": "text", "text": "正文第一段,句尾没有标点的话很容易被误判为断句。",
         "page_idx": 1, "bbox": [10, 20, 300, 40]},
        {"type": "text", "text": "第二段**加粗**与 `行内代码` 混排,以及 x^2+y^2 行内公式。",
         "page_idx": 2, "bbox": [10, 60, 300, 90]},
        {"type": "page_number", "text": "12", "page_idx": 2, "bbox": [150, 400, 170, 420]},
    ]


def test_evidence_block_refuses_any_rewrite() -> None:
    ir = _ir(evidence=_evidence_items())
    evidence = ir.evidence_blocks[0]
    assert evidence.editable is False

    with pytest.raises(ReadonlyBlockError) as excinfo:
        ir.rewrite(evidence.id, "偷偷改掉证据")
    assert excinfo.value.block_id == evidence.id
    # 正文一个字符都没变
    assert render_ir(ir) == SYNTHETIC_MD


def test_evidence_blocks_never_reach_the_rendered_markdown() -> None:
    ir = _ir(evidence=_evidence_items())
    assert ir.evidence_blocks
    assert render_ir(ir) == SYNTHETIC_MD


# ---------------------------------------------------------------- 落盘确定性

def test_dump_ir_is_deterministic(tmp_path) -> None:
    ir = _ir(evidence=_evidence_items())
    first = dump_ir(ir, tmp_path / "a")
    second = dump_ir(ir, tmp_path / "b")
    for name in ("meta.json", "blocks.jsonl", "links.jsonl"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name

    lines = (first / "blocks.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(ir.blocks)
    rows = [json.loads(line) for line in lines]
    # 可编辑块在前,各层按 order 升序
    assert [r["editable"] for r in rows] == sorted((r["editable"] for r in rows), reverse=True)
    assert rows[0]["id"] == ir.editable_blocks[0].id


def test_dump_then_load_renders_the_same_text(tmp_path) -> None:
    ir = _ir(evidence=_evidence_items())
    dump_ir(ir, tmp_path)
    restored = load_ir(tmp_path)
    assert render_ir(restored) == SYNTHETIC_MD
    assert restored.counts() == ir.counts()
    assert restored.meta.counts == ir.meta.counts
    assert [block_to_dict(b) for b in restored.blocks] == [block_to_dict(b) for b in ir.blocks]


def test_load_ir_refuses_unknown_schema(tmp_path) -> None:
    dump_ir(_ir(), tmp_path)
    meta_path = ir_dir(tmp_path) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["schema"] = 99
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(IRError):
        load_ir(tmp_path)


def test_load_ir_without_meta_is_an_error(tmp_path) -> None:
    with pytest.raises(IRError):
        load_ir(tmp_path)


# ---------------------------------------------------------------- 真实缓存书(本地可选)

CACHED_BOOKS = sorted(p for p in (PROJECT_ROOT / "work").glob("*/book.md"))


@pytest.mark.parametrize("md_path", CACHED_BOOKS, ids=lambda p: p.parent.name[:24] or "book")
def test_real_cached_book_roundtrips(md_path) -> None:
    """三本真实缓存书 + 样本:往返逐字节一致、切片铺满全文(read-only)。"""
    md = md_path.read_text(encoding="utf-8")
    ir = build_ir(md, source=str(md_path), backend="mineru", total_pages=0)
    assert render_ir(ir) == md
    ok, why = spans_tile(ir, md)
    assert ok, why
