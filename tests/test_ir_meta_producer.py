"""IR 元信息里的来源与构建信息(阶段 B §K.6 遗留项)。

P1 的 ``meta.json`` 只有 ``backend`` 与 ``source``,复现脚本得靠猜「这本书的 md 是
谁产出的」。这一组测试守三条:

  - 说得出来的来源必须写进去(producer/source/source_sha256/variant);
  - 说不出来的**写 unknown**,不许拿 ``backend`` 顶替 —— 回放场景里
    "这次用哪个后端"与"这份 md 当年谁写的"根本不是同一件事;
  - P1 写的老 ``meta.json``(没有 producer)必须能读,读出来是 unknown。
"""
from __future__ import annotations

import json

import pytest

from ir import (IR_SCHEMA, IRError, Producer, build_ir, dump_ir, ir_dir,
                load_ir, producer_from_dict, producer_to_dict, render_ir)

MD = "# 第一章 起头\n\n正文第一段,句尾没有标点。\n"


def _build(**kwargs):
    kwargs.setdefault("source", "synthetic.md")
    kwargs.setdefault("backend", "mineru")
    return build_ir(MD, **kwargs)


def test_missing_producer_marks_unknown_instead_of_inferring() -> None:
    meta = _build().meta
    assert meta.producer.name == "unknown"          # 不拿 backend 顶替
    assert meta.backend == "mineru"                 # backend 仍是调用方给的事实
    assert meta.producer.source == "synthetic.md"   # 来源是入参,照记
    assert meta.producer.variant == "original"
    assert meta.producer.tool == "pdf2epub"
    assert meta.producer.python                     # 构建信息:解释器版本
    assert meta.producer.ir_schema == IR_SCHEMA


def test_explicit_producer_survives_dump_and_load(tmp_path) -> None:
    producer = Producer.from_backend("paddleocr", source="work/book/source.pdf",
                                     source_sha256="a" * 64,
                                     variant="rendered:1a2b3c")
    ir = _build(producer=producer)
    dump_ir(ir, tmp_path)
    loaded = load_ir(tmp_path)

    assert loaded.meta.producer == producer
    assert loaded.meta.producer.name == "paddleocr"
    assert render_ir(loaded) == MD


def test_meta_json_contains_the_producer_block(tmp_path) -> None:
    dump_ir(_build(producer=Producer.from_backend("mineru", source="s.pdf")), tmp_path)
    data = json.loads((ir_dir(tmp_path) / "meta.json").read_text(encoding="utf-8"))
    assert list(data["producer"]) == [
        "name", "source", "source_sha256", "variant", "tool", "tool_version",
        "python", "ir_schema",
    ]
    assert data["producer"]["name"] == "mineru"


def test_old_meta_without_producer_still_loads_as_unknown(tmp_path) -> None:
    dump_ir(_build(), tmp_path)
    meta_path = ir_dir(tmp_path) / "meta.json"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data.pop("producer")                      # 模拟 P1 产出的 meta.json
    meta_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    loaded = load_ir(tmp_path)
    assert loaded.meta.producer.name == "unknown"
    assert loaded.meta.producer.name != loaded.meta.backend
    assert render_ir(loaded) == MD


def test_producer_normalizes_blanks_and_prose_to_unknown() -> None:
    blank = Producer(name="  ", source="", variant="\t")
    assert blank.name == "unknown"
    assert blank.source == "unknown"
    assert blank.variant == "unknown"
    assert not blank.known
    assert Producer(name="mineru").known


def test_producer_refuses_a_schema_from_the_future() -> None:
    with pytest.raises(IRError):
        Producer(ir_schema=IR_SCHEMA + 1)


def test_producer_dict_roundtrips() -> None:
    producer = Producer.from_backend("mineru", source="a.pdf", source_sha256="b" * 64)
    assert producer_from_dict(producer_to_dict(producer)) == producer
    # 字符串写法也认(将来可能简化成只写名字),其余字段一律 unknown
    assert producer_from_dict("mineru").name == "mineru"
    assert producer_from_dict(None).name == "unknown"


def test_dump_stays_deterministic_with_producer(tmp_path) -> None:
    ir = _build(producer=Producer.from_backend("mineru", source="s.pdf"))
    first, second = tmp_path / "a", tmp_path / "b"
    dump_ir(ir, first)
    dump_ir(ir, second)
    assert (ir_dir(first) / "meta.json").read_bytes() == \
        (ir_dir(second) / "meta.json").read_bytes()
