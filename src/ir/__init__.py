"""IR(中间表示)子包 —— 阶段 B 方案 §E 的实现。

对外只暴露"入口级"名字,内部模块可以自由重构:

    from ir import build_ir, render_ir, dump_ir, load_ir

设计要点(详见各模块文档):

- ``model``   数据模型与硬约束(证据只读、bbox 与坐标系同生同死、不猜页号)
- ``parse``   Markdown → 可编辑块;``content_list`` → 证据块;``build_ir`` 组装
- ``align``   文本锚点 + 单调游标对齐,配不上就空着
- ``render``  按块序原样拼回,保证逐字节往返
- ``store``   确定性落盘 / 只读回读
- ``pages``   页归属审计(P4/P5):只统计与报问题,不补页号;零归属页按证据层分成
  blank / no_text / unattributed / unavailable 四类
"""
from __future__ import annotations

from ir.align import ANCHOR_CHARS, align_blocks, block_anchor, links_monotonic
from ir.model import (IR_SCHEMA, UNKNOWN, Block, BookIR, DocMeta, IRError, Link,
                      Producer, Provenance, ReadonlyBlockError, text_sha256)
from ir.pages import (GAP_STATES, ISSUE_CODES, SEVERITIES, PageAttribution,
                      PageAuditError, PageGap, PageIssue, audit_many,
                      audit_page_attribution, covered_pages,
                      format_attribution, gaps_by_state, page_coverage,
                      page_gaps, uncovered_pages)
from ir.parse import ParsedMarkdown, build_ir, parse_evidence, parse_markdown
from ir.render import (RoundtripError, assert_roundtrip, first_divergence,
                       render_blocks, render_ir, roundtrip_ok, spans_tile)
from ir.store import (IR_DIR, block_from_dict, block_to_dict, dump_ir, ir_dir,
                      load_ir, producer_from_dict, producer_to_dict)

__all__ = [
    "IR_SCHEMA", "IR_DIR", "ANCHOR_CHARS", "UNKNOWN",
    "Block", "BookIR", "DocMeta", "Link", "Provenance", "Producer",
    "IRError", "ReadonlyBlockError", "RoundtripError",
    "ParsedMarkdown", "parse_markdown", "parse_evidence", "build_ir",
    "align_blocks", "block_anchor", "links_monotonic",
    "render_ir", "render_blocks", "assert_roundtrip", "roundtrip_ok",
    "spans_tile", "first_divergence",
    "dump_ir", "load_ir", "ir_dir", "block_to_dict", "block_from_dict",
    "producer_to_dict", "producer_from_dict",
    "text_sha256",
    # --- P4 页归属审计 ---
    "SEVERITIES", "ISSUE_CODES", "PageIssue", "PageAttribution", "PageAuditError",
    "audit_page_attribution", "audit_many", "page_coverage", "format_attribution",
    "covered_pages", "uncovered_pages",
    # --- P5 零归属成因 ---
    "GAP_STATES", "PageGap", "page_gaps", "gaps_by_state",
]
