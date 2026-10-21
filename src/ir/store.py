"""IR 落盘(阶段 B §E.6)。

```
work/<book>/ir/
  meta.json      schema/来源/页数/变体/统计
  blocks.jsonl   一行一块,按 order 排序
  links.jsonl    一行一 link
```

两条硬约束:

1. **确定性**:同一 IR 两次落盘字节相同 —— 字段顺序固定、无浮点、
   ``ensure_ascii=False``、``\n`` 换行、末尾一个换行。差分测试才可用。
2. **只读回读**:``load_ir`` 只还原现有文件,不做任何修复或补全;版本不符直接拒绝
   (猜版本的后果是拿错字段当页号,比报错危险得多)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ir.model import (IR_SCHEMA, UNKNOWN, Block, BookIR, DocMeta, IRError, Link,
                      Producer, Provenance)

IR_DIR = "ir"
META_FILE = "meta.json"
BLOCKS_FILE = "blocks.jsonl"
LINKS_FILE = "links.jsonl"


def ir_dir(work_dir: str | Path) -> Path:
    return Path(work_dir) / IR_DIR


def _prov_dict(prov: Provenance) -> dict:
    return {
        "backend": prov.backend,
        "part_id": prov.part_id,
        "block_index": prov.block_index,
        "page_idx": prov.page_idx,
        "page_idx_source": prov.page_idx_source,
        "raw_type": prov.raw_type,
        "bbox": list(prov.bbox) if prov.bbox else None,
        "bbox_space": prov.bbox_space,
        "text_level": prov.text_level,
        "offset": prov.offset,
    }


def block_to_dict(block: Block) -> dict:
    """固定字段顺序(差分友好);``extra`` 里的 trailer 保持原文。"""
    return {
        "id": block.id,
        "kind": block.kind,
        "order": block.order,
        "editable": block.editable,
        "kind_confidence": block.kind_confidence,
        "level": block.level,
        "md_span": list(block.md_span) if block.md_span else None,
        "prov": _prov_dict(block.prov),
        "text": block.text,
        "extra": block.extra,
    }


def block_from_dict(data: dict) -> Block:
    prov = data.get("prov") or {}
    span = data.get("md_span")
    return Block(
        id=str(data["id"]),
        kind=data["kind"],
        text=str(data.get("text", "")),
        order=int(data["order"]),
        prov=Provenance(
            backend=str(prov.get("backend", "")),
            part_id=str(prov.get("part_id", "")),
            block_index=int(prov.get("block_index", 0)),
            page_idx=prov.get("page_idx"),
            page_idx_source=str(prov.get("page_idx_source") or "none"),
            raw_type=str(prov.get("raw_type") or ""),
            bbox=tuple(prov["bbox"]) if prov.get("bbox") else None,
            bbox_space=str(prov.get("bbox_space") or ""),
            text_level=prov.get("text_level"),
            offset=prov.get("offset"),
        ),
        kind_confidence=data["kind_confidence"],
        editable=bool(data["editable"]),
        md_span=(int(span[0]), int(span[1])) if span else None,
        level=data.get("level"),
        extra=dict(data.get("extra") or {}),
    )


def link_to_dict(link: Link) -> dict:
    return {
        "block_id": link.block_id,
        "evidence_id": link.evidence_id,
        "confidence": link.confidence,
        "how": link.how,
    }


def link_from_dict(data: dict) -> Link:
    return Link(
        block_id=str(data["block_id"]),
        evidence_id=str(data["evidence_id"]),
        confidence=data["confidence"],
        how=str(data.get("how") or "anchor"),
    )


def producer_to_dict(producer: Producer) -> dict:
    return {
        "name": producer.name,
        "source": producer.source,
        "source_sha256": producer.source_sha256,
        "variant": producer.variant,
        "tool": producer.tool,
        "tool_version": producer.tool_version,
        "python": producer.python,
        "ir_schema": producer.ir_schema,
    }


def producer_from_dict(data: object) -> Producer:
    """读 ``meta.producer``;**缺失即 unknown,不推断**。

    P1 写的 ``meta.json`` 没有这个字段,读的时候必须能过 —— 但也不能拿
    ``meta.backend`` 去补 ``producer.name``:两份产物可能不是同一个后端写的
    (回放、换后端重跑、混合段),猜出来的来源比"未知"更糟。
    """
    if isinstance(data, str) and data.strip():
        return Producer(name=data)
    if not isinstance(data, dict):
        return Producer()
    return Producer(
        name=str(data.get("name") or UNKNOWN),
        source=str(data.get("source") or UNKNOWN),
        source_sha256=str(data.get("source_sha256") or UNKNOWN),
        variant=str(data.get("variant") or UNKNOWN),
        tool=str(data.get("tool") or "pdf2epub"),
        tool_version=str(data.get("tool_version") or UNKNOWN),
        python=str(data.get("python") or UNKNOWN),
        ir_schema=int(data.get("ir_schema") or IR_SCHEMA),
    )


def meta_to_dict(meta: DocMeta) -> dict:
    return {
        "schema": meta.schema,
        "source": meta.source,
        "backend": meta.backend,
        "producer": producer_to_dict(meta.producer),
        "total_pages": meta.total_pages,
        "params_fp": meta.params_fp,
        "variant": meta.variant,
        "created_at": meta.created_at,
        "prefix": meta.prefix,
        "backend_evidence": meta.backend_evidence,
        "counts": meta.counts,
    }


def meta_from_dict(data: dict) -> DocMeta:
    schema = data.get("schema")
    if schema != IR_SCHEMA:
        raise IRError(f"IR schema 不支持: {schema}(本实现只认 {IR_SCHEMA})")
    return DocMeta(
        source=str(data.get("source") or ""),
        backend=str(data.get("backend") or ""),
        total_pages=int(data.get("total_pages") or 0),
        params_fp=str(data.get("params_fp") or ""),
        created_at=int(data.get("created_at") or 0),
        schema=int(schema),
        variant=str(data.get("variant") or "original"),
        backend_evidence=dict(data.get("backend_evidence") or {}),
        prefix=str(data.get("prefix") or ""),
        counts=dict(data.get("counts") or {}),
        producer=producer_from_dict(data.get("producer")),
    )


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    body = "".join(_dumps(row) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")


def dump_ir(ir: BookIR, work_dir: str | Path) -> Path:
    """把 IR 写到 ``<work_dir>/ir/``;返回目录。同输入两次写字节相同。"""
    out = ir_dir(work_dir)
    out.mkdir(parents=True, exist_ok=True)
    blocks = sorted(ir.blocks, key=lambda b: (not b.editable, b.order))
    _write_jsonl(out / BLOCKS_FILE, (block_to_dict(b) for b in blocks))
    _write_jsonl(out / LINKS_FILE, (link_to_dict(l) for l in ir.links))
    (out / META_FILE).write_text(_dumps(meta_to_dict(ir.meta)) + "\n", encoding="utf-8")
    return out


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def load_ir(work_dir: str | Path) -> BookIR:
    """读回 IR(不做修复;缺 meta 或版本不符直接抛)。"""
    root = ir_dir(work_dir)
    meta_path = root / META_FILE
    if not meta_path.is_file():
        raise IRError(f"没有 IR 产物: {meta_path}")
    meta = meta_from_dict(json.loads(meta_path.read_text(encoding="utf-8")))
    blocks = tuple(block_from_dict(row) for row in _read_jsonl(root / BLOCKS_FILE))
    links = tuple(link_from_dict(row) for row in _read_jsonl(root / LINKS_FILE))
    pages = tuple(range(1, meta.total_pages + 1)) if meta.total_pages else ()
    ir = BookIR(meta=meta, blocks=blocks, links=links, pages=pages)
    ir.validate()
    return ir
