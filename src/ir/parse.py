"""把产物切成 IR(阶段 B §E.1 / §E.4 / §E.5)。

两件事分开做,互不掺和:

``parse_markdown(md)``
    纯文本 → Layer1 可编辑块。切分只认**结构标记**(空行、``#`` 标题、围栏代码、
    表格、列表项、公式、图片行、脚注定义、HTML 注释),**不改一个字符**:
    每个块 ``text`` 就是 ``md[span[0]:span[1]]``,块之间的空白原样记在
    ``extra['trailer']``。所以 ``render(parse(md)) == md`` 是构造出来的,不是测出来的。

``parse_evidence(items)``
    ``content_list.json`` → Layer2 证据块(只读)。只做类型映射与坐标系标注,
    不合并、不推断页号。

``build_ir(...)``
    把两层拼起来跑对齐,产出 ``BookIR``。

约定:``BookIR.blocks`` 里两层共存(便于序列化),但 ``order`` 是**每层各自**
的阅读顺序 —— Layer1 的 order 是 md 段序,Layer2 的 order 是后端块序。
跨层比较没有意义,这正是不拿 order 做对齐、而用文本锚点的原因。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

from ir.align import align_blocks
from ir.model import (IR_SCHEMA, UNKNOWN, Block, BookIR, DocMeta, IRError,
                      Producer, Provenance, iter_text, text_sha256)

# ---------------------------------------------------------------- 行级模式

_HEADING_RE = re.compile(r"^(#{1,6})(?:[ \t]|$)")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_HR_RE = re.compile(r"^(?:\*[ \t]*){3,}$|^(?:-[ \t]*){3,}$|^(?:_[ \t]*){3,}$")
_TABLE_RE = re.compile(r"^\|")
_LIST_RE = re.compile(r"^([-*+]|\d{1,3}[.)])[ \t]+\S")
_MATH_RE = re.compile(r"^(\$\$|\\\[)")
_FOOTDEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:(.*)$")
_IMAGE_ONLY_RE = re.compile(r"^!\[[^\]]*\]\(([^)]+)\)$")
_COMMENT_RE = re.compile(r"^<!--(.*?)(?:-->)?$", re.S)
_PAGE_MARKER_RE = re.compile(r"^<!--\s*page\s+(\d+)\s*-->$", re.I)


@dataclass(frozen=True)
class ParsedMarkdown:
    """切分结果。``prefix`` 是文首空白(往返必须带上)。"""

    prefix: str
    blocks: tuple[Block, ...]


def _strip_eol(line: str) -> tuple[str, str]:
    """``(不含行尾符的内容, 行尾符)``;兼容 CRLF/CR。"""
    for eol in ("\r\n", "\n", "\r"):
        if line.endswith(eol):
            return line[: -len(eol)], eol
    return line, ""


def _is_blank(line: str) -> bool:
    return not line.strip()


def _starts_block(line: str) -> bool:
    """这一行会不会开启一个新块(段落遇到它就收尾)。"""
    if not line or line != line.lstrip():
        return False                      # 缩进行是上一块的续行
    if _HEADING_RE.match(line) or _FENCE_RE.match(line):
        return True
    if line.startswith("<!--") or _TABLE_RE.match(line):
        return True
    if _HR_RE.match(line) or _MATH_RE.match(line):
        return True
    if _FOOTDEF_RE.match(line) or _LIST_RE.match(line):
        return True
    return bool(_IMAGE_ONLY_RE.match(line))


class _Builder:
    """按行扫描,产出一块并记住它的原始区间。"""

    def __init__(self, md: str) -> None:
        self.md = md
        self.lines = md.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.lines:
            self.offsets.append(self.offsets[-1] + len(line))

    # --- 区间工具 ---
    def text_upto(self, start_line: int, end_line: int) -> tuple[str, str]:
        """返回 ``(正文, trailer)``:正文不含最后一行行尾符,trailer 从行尾符起算。"""
        start = self.offsets[start_line]
        stop = self.offsets[end_line]
        body, eol = _strip_eol(self.lines[end_line - 1])
        text = self.md[start:stop - len(eol)]
        trailer = self.md[stop - len(eol):stop]
        nxt = end_line
        while nxt < len(self.lines) and _is_blank(self.lines[nxt]):
            trailer += self.lines[nxt]
            nxt += 1
        return text, trailer

    def consume_trailer(self, end_line: int) -> int:
        """跳过尾随空行,返回下一块的行号。"""
        while end_line < len(self.lines) and _is_blank(self.lines[end_line]):
            end_line += 1
        return end_line

    # --- 定位 ---
    def first_content_line(self) -> int:
        i = 0
        while i < len(self.lines) and _is_blank(self.lines[i]):
            i += 1
        return i


def parse_markdown(md: str, *, backend: str = "ir", part_id: str = "book",
                   order_start: int = 0) -> ParsedMarkdown:
    """Markdown → 可编辑块(逐字节可还原)。

    块 id 形如 ``<part_id>#m12``(m = markdown 层,与证据层的 ``#e12`` 区分,
    两层 id 不会撞)。
    """
    if not isinstance(md, str):
        raise IRError("parse_markdown 只接受 str")
    builder = _Builder(md)
    lines = builder.lines
    start_line = builder.first_content_line()
    prefix = md[: builder.offsets[start_line]] if start_line < len(lines) else md

    blocks: list[Block] = []
    i = start_line
    index = 0
    while i < len(lines):
        content, _eol = _strip_eol(lines[i])
        if _is_blank(content):
            i += 1
            continue
        kind, level, extra, cls = _classify(content)
        j = i + 1
        if cls == "fence":
            fence = _FENCE_RE.match(content)
            mark = fence.group(1) if fence else "```"
            while j < len(lines):
                nxt, _ = _strip_eol(lines[j])
                j += 1
                if nxt.startswith(mark[:3]) and nxt.strip() == mark:
                    break
        elif cls == "comment":
            if "-->" not in content:
                while j < len(lines):
                    nxt, _ = _strip_eol(lines[j])
                    j += 1
                    if "-->" in nxt:
                        break
        elif cls == "math":
            if not (content.rstrip().endswith("$$") and len(content.strip()) > 4):
                closer = "$$" if content.lstrip().startswith("$$") else "\\]"
                closed = False
                while j < len(lines):
                    nxt, _ = _strip_eol(lines[j])
                    j += 1
                    if nxt.rstrip().endswith(closer):
                        closed = True
                        break
                if not closed:
                    # 找不到收尾定界行 → 这不是数学块(可能是正文里字面出现的 `$`/
                    # `$$`)。把后面整本书都算进这个块会让那些页**静默失去清理** ——
                    # 宁可漏保护一处公式,也不能让清理器形同失效。
                    j = i + 1
                    kind, level, extra, cls = "text", None, {}, "paragraph"
        elif cls == "table":
            while j < len(lines):
                nxt, _ = _strip_eol(lines[j])
                if not nxt.strip().startswith("|"):
                    break
                j += 1
        elif cls in ("list", "footdef"):
            while j < len(lines):
                nxt, _ = _strip_eol(lines[j])
                if _is_blank(nxt) or nxt == nxt.lstrip():
                    break
                j += 1
        elif cls in ("heading", "hr", "image"):
            pass                                  # 单行块
        else:                                     # 段落
            while j < len(lines):
                nxt, _ = _strip_eol(lines[j])
                if _is_blank(nxt) or _starts_block(nxt):
                    break
                j += 1

        text, trailer = builder.text_upto(i, j)
        extra = dict(extra)
        extra["trailer"] = trailer
        start = builder.offsets[i]
        prov = Provenance(backend=backend, part_id=part_id, block_index=index)
        blocks.append(Block(
            id=f"{part_id}#m{index}",
            kind=kind,                                   # type: ignore[arg-type]
            text=text,
            order=order_start + index,
            prov=prov,
            kind_confidence="explicit",
            editable=True,
            md_span=(start, start + len(text)),
            level=level,
            extra=extra,
        ))
        index += 1
        i = j

    return ParsedMarkdown(prefix=prefix, blocks=tuple(blocks))


def _classify(content: str) -> tuple[str, int | None, dict, str]:
    """``(kind, level, extra, 切分类别)``。只认结构标记,不做语义猜测。"""
    heading = _HEADING_RE.match(content)
    if heading:
        level = len(heading.group(1))
        return "heading", level, {"marker": heading.group(1)}, "heading"
    if _FENCE_RE.match(content):
        mark = _FENCE_RE.match(content).group(1)      # type: ignore[union-attr]
        return "code", None, {"fence": mark}, "fence"
    if content.startswith("<!--"):
        marker = _PAGE_MARKER_RE.match(content.strip())
        if marker:
            return "unknown", None, {"role": "page_marker", "page": int(marker.group(1))}, "comment"
        return "unknown", None, {"role": "html_comment"}, "comment"
    if _HR_RE.match(content):
        return "unknown", None, {"role": "horizontal_rule"}, "hr"
    if _MATH_RE.match(content):
        return "equation", None, {}, "math"
    if _TABLE_RE.match(content):
        return "table", None, {}, "table"
    foot = _FOOTDEF_RE.match(content)
    if foot:
        return "footnote", None, {"marker": f"[^{foot.group(1)}]"}, "footdef"
    listed = _LIST_RE.match(content)
    if listed:
        return "list_item", None, {"marker": listed.group(1)}, "list"
    image = _IMAGE_ONLY_RE.match(content)
    if image:
        return "image", None, {"image_ref": image.group(1)}, "image"
    return "text", None, {}, "paragraph"


# ---------------------------------------------------------------- 证据层

_MINERU_KIND = {
    "text": "text",
    "page_number": "page_number",
    "header": "header",
    "footer": "footer",
    "page_footnote": "footnote",
    "ref_text": "reference",
    "table": "table",
    "equation": "equation",
    "code": "code",
    "list": "list",
    "image": "image",
}

#: PaddleOCR-VL 的 label → (kind, 可推的标题级别)
_PADDLE_LABEL = {
    "doc_title": ("heading", 1),
    "title": ("heading", None),
    "paragraph_title": ("heading", None),
    "text": ("text", None),
    "number": ("page_number", None),
    "header": ("header", None),
    "footer": ("footer", None),
    "aside_text": "unknown",
    "formula": "equation",
    "table": "table",
    "image": "image",
    "footnote": ("footnote", None),
    "page_footnote": ("footnote", None),       # 没带 label 时的兜底(type 原值)
}

#: 这些类型的块只是页界/排版标记,不当作内容证据
_SKIP_TYPES = frozenset({"page_break", "discarded", "page_group"})


def _bbox(raw: object) -> tuple[tuple[float, float, float, float] | None, str]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None, ""
    try:
        return tuple(float(v) for v in raw), ""      # type: ignore[return-value]
    except (TypeError, ValueError):
        return None, ""


def parse_evidence(items: Iterable[object], *, backend: str, part_id: str,
                   page_offset: int = 0, order_start: int = 0) -> tuple[Block, ...]:
    """``content_list.json`` → 只读证据块。

    ``page_offset``:该段首页在整本 PDF 里的 0-based 偏移(分片时 = range 起点 - 1)。
    没有页号的块 ``page_idx=None``,不按顺序补 —— 补出来的页号会让页覆盖校验失效。
    """
    if isinstance(items, dict):                      # 容错:有的后端包一层
        items = items.get("blocks") or items.get("content_list") or []
    blocks: list[Block] = []
    for idx, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        # PaddleOCR 的 type 恒为 "text"/"page_footnote",语义在 label 里(doc_title 等);
        # MinerU 反过来只有 type。先取 type,但 Paddle 时以 label 为准。
        raw_type = str(item.get("type") or item.get("label") or "")
        if backend == "paddleocr" and item.get("label"):
            raw_type = str(item["label"])
        if raw_type in _SKIP_TYPES or item.get("type") in _SKIP_TYPES:
            continue
        kind, level, confidence = _map_kind(raw_type, backend, item)
        page = item.get("page_idx")
        page_idx = page + page_offset if isinstance(page, int) and page >= 0 else None
        box, space = _bbox(item.get("bbox"))
        space = space or ("page_px" if backend == "mineru" else
                          "norm1000" if backend == "paddleocr" else "")
        text = item.get("text")
        if not isinstance(text, str):
            body = item.get("table_body") or item.get("content") or ""
            text = body if isinstance(body, str) else ""
        extra: dict = {"img_path": item.get("img_path") or ""}
        for key in ("image_caption", "image_footnote", "table_body", "img_path", "label"):
            if item.get(key):
                extra[key] = item[key]
        prov = Provenance(
            backend=backend, part_id=part_id, block_index=idx,
            page_idx=page_idx,
            page_idx_source="backend" if page_idx is not None else "none",
            raw_type=raw_type, bbox=box, bbox_space=space,
            text_level=item.get("text_level") if isinstance(item.get("text_level"), int) else None,
        )
        blocks.append(Block(
            id=f"{part_id}#e{idx}",
            kind=kind,                                  # type: ignore[arg-type]
            text=text,
            order=order_start + len(blocks),
            prov=prov,
            kind_confidence=confidence,                 # type: ignore[arg-type]
            editable=False,
            level=level,
            extra=extra,
        ))
    return tuple(blocks)


def _map_kind(raw_type: str, backend: str, item: dict) -> tuple[str, int | None, str]:
    if backend == "paddleocr":
        mapped = _PADDLE_LABEL.get(raw_type, "text")
        if isinstance(mapped, tuple):
            kind, level = mapped
        else:
            kind, level = mapped, None
        return kind, level, "derived" if kind == "heading" else "explicit"
    kind = _MINERU_KIND.get(raw_type, "text")
    level = None
    if kind == "text" and isinstance(item.get("text_level"), int):
        kind = "heading"                              # text_level 是显式层级
        level = int(item["text_level"])
    return kind, level, "explicit"


# ---------------------------------------------------------------- 组装

def build_ir(md: str, *, source: str = "", backend: str = "ir",
             total_pages: int = 0, params_fp: str = "", variant: str = "original",
             evidence: Sequence[object] | None = None, part_id: str = "book",
             page_offset: int = 0, created_at: int = 0,
             strict_pages: bool = False,
             backend_evidence: dict | None = None,
             producer: Producer | None = None) -> BookIR:
    """Markdown(+可选 ``content_list``)→ ``BookIR``。

    ``strict_pages=True`` 时任何可编辑块缺页归属即抛 ``IRError``(§E.5 不变量 7)。
    P1 默认 **False**:先把对齐失败变成可观测数据,再由 P5 决定是否收紧闸门。

    ``producer`` 说明这份 md 是谁产出的。不传时**不猜**:``name`` 记 ``unknown``,
    只把调用方确实给了的 ``source`` / ``variant`` 原样记下(那是入参,不是推断)。
    """
    parsed = parse_markdown(md, backend=backend, part_id=part_id)
    editable: tuple[Block, ...] = parsed.blocks
    evidence_blocks = parse_evidence(
        evidence or [], backend=backend, part_id=part_id, page_offset=page_offset,
    ) if evidence else ()

    links: tuple = ()
    stats = {"evidence_available": bool(evidence_blocks), "anchor_hits": 0,
             "containment_hits": 0, "interval_attributed": 0, "unmatched": 0,
             "out_of_order": 0, "page_attribution_missing": 0}
    if evidence_blocks:
        editable, links, stats = align_blocks(editable, evidence_blocks)

    pages = tuple(range(1, total_pages + 1)) if total_pages > 0 else ()
    evidence_meta = dict(backend_evidence or {})
    evidence_meta["page_attribution"] = (
        "complete" if evidence_blocks and not stats["page_attribution_missing"]
        else "partial" if evidence_blocks
        else "unavailable"
    )
    meta = DocMeta(
        source=source,
        backend=backend,
        total_pages=total_pages,
        params_fp=params_fp,
        created_at=created_at,
        schema=IR_SCHEMA,
        variant=variant,
        backend_evidence=evidence_meta,
        prefix=parsed.prefix,
        counts={},
        producer=producer if producer is not None else Producer(
            name=UNKNOWN,
            source=source or UNKNOWN,
            variant=variant or UNKNOWN,
        ),
    )
    ir = BookIR(meta=meta, blocks=editable + evidence_blocks, links=links, pages=pages)
    ir = type(ir)(meta=_with_counts(ir, stats), blocks=ir.blocks, links=ir.links, pages=ir.pages)
    ir.validate()
    if strict_pages:
        ir.verify_page_attribution()
    return ir


def _with_counts(ir: BookIR, stats: dict) -> DocMeta:
    counts = ir.counts()
    counts.update({k: v for k, v in stats.items() if k != "page_attribution_missing"})
    counts["page_attribution_missing"] = ir.page_attribution_missing
    editable = sorted(ir.editable_blocks, key=lambda b: b.order)
    counts["md_sha256"] = text_sha256(ir.meta.prefix + iter_text(editable))
    return replace(ir.meta, counts=counts)
