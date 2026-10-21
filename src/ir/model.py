"""IR 中间表示的数据模型(阶段 B §E.2 / §E.3,v2 契约)。

三层结构:

    Layer 1  editable blocks  从 book.md 切出的可编辑片段,``text`` 就是 md 原文切片
    Layer 2  evidence blocks  来自 content_list 的结构事实,只读、不参与文本改写
    Layer 3  links            1↔2 的对齐关系(带置信度)

为什么 ``text`` 是"原文切片"而不是"规整后的正文":往返必须**逐字节**一致
(``render(parse(book.md)) == book.md``,P1 验收闸门)。任何在解析期做的规整
(去空格、补/去标记)都会让产物漂移,而这正是本次重构要避免的。规则想改文本时
走 Patch(阶段 P2),不在这里偷偷改。

硬约束(每条都有测试):

1. ``Block.text`` 与 ``md_span`` 严格对应(``len(text) == end - start``)。
2. ``editable=False`` 的块不得持有 ``md_span``;任何改写请求抛 ``ReadonlyBlockError``。
3. ``bbox`` 与 ``bbox_space`` 同时有或同时无 —— 只有 bbox 没有坐标系等于没有 bbox
   (不假装不同后端的坐标是同一个坐标系)。
4. 不猜页号:拿不到就是 ``None``,``page_idx_source`` 说明它凭什么得来。
"""
from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass, field, replace
from typing import Iterable, Literal

#: IR 落盘格式版本(与 ``clean-report.json`` 的 schema_version 是两件事)
IR_SCHEMA = 1

Kind = Literal[
    "text", "heading", "page_number", "header", "footer", "footnote",
    "reference", "table", "equation", "code", "image", "list", "list_item",
    "toc", "verse", "unknown",
]
Confidence = Literal["explicit", "derived", "heuristic"]

KINDS: frozenset[str] = frozenset(Kind.__args__)                      # type: ignore[attr-defined]
CONFIDENCES: frozenset[str] = frozenset(Confidence.__args__)          # type: ignore[attr-defined]

#: ``prov.page_idx_source`` 的合法取值。空串会在构造时归一为 ``"none"``。
PAGE_SOURCES: frozenset[str] = frozenset(
    {"backend", "page_range", "page_order", "interval", "none"}
)

#: 允许携带 ``bbox`` 的坐标系。不在这里的坐标系一律按"没有 bbox"处理。
BBOX_SPACES: frozenset[str] = frozenset({"page_px", "norm1000", "pt"})


class IRError(ValueError):
    """IR 构造/校验失败(fail fast —— 不产出半成品 IR)。"""


class ReadonlyBlockError(IRError):
    """试图改写证据块(``editable=False``)。

    阶段 B §E.5 不变量 3:这类请求必须被拒绝并计数,不能静默忽略 ——
    否则"规则改了正文却没进报告"这类事故无从追溯。
    """

    def __init__(self, block_id: str, kind: str) -> None:
        super().__init__(f"块 {block_id}(kind={kind})是证据块,不可改写")
        self.block_id = block_id
        self.kind = kind


def text_sha256(text: str) -> str:
    """``Patch.before/after.sha256`` 用的就是它(UTF-8 字节)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: 所有「说不出来」的取值统一写成 ``unknown``,而不是空串。
#: 空串与"未知"在读端没法区分,而这两件事的处置完全不同(空串像"没有"),
#: 所以宁可显式写 unknown。
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Producer:
    """这份 Markdown 是谁产出的、从哪来、什么版本的工具和解释器。

    P1 的 ``meta.json`` 只有 ``backend`` 与 ``source`` 两个字段,复现脚本要靠猜
    「这本书的 md 是哪个后端、哪个变体、哪次输入产出的」。这里把它写成显式记录:

    - ``name``:产出方 —— ``mineru`` / ``paddleocr`` / ``pymupdf`` / ``markdown``。
      说不出来就是 ``unknown``,**不从 ``backend`` 推断**:同一条回放流程里
      ``backend`` 是"这次用哪个后端",与"这份 md 当年是谁写的"不是同一件事。
    - ``source`` / ``source_sha256``:输入文件及其内容指纹;回放时据此判断
      "这份 IR 对应的还是不是同一个 PDF"。
    - ``tool`` / ``tool_version`` / ``python`` / ``ir_schema``:构建信息。
    """

    name: str = UNKNOWN
    source: str = UNKNOWN
    source_sha256: str = UNKNOWN
    variant: str = UNKNOWN
    tool: str = "pdf2epub"
    tool_version: str = UNKNOWN
    python: str = field(default_factory=platform.python_version)
    ir_schema: int = IR_SCHEMA

    def __post_init__(self) -> None:
        for key in ("name", "source", "source_sha256", "variant", "tool", "tool_version"):
            value = getattr(self, key)
            object.__setattr__(self, key, str(value).strip() if str(value).strip() else UNKNOWN)
        if self.ir_schema != IR_SCHEMA:
            raise IRError(f"producer.ir_schema 与 IR_SCHEMA 不一致: {self.ir_schema}")

    @property
    def known(self) -> bool:
        return self.name != UNKNOWN

    @classmethod
    def from_backend(cls, backend: str, *, source: str = UNKNOWN,
                     source_sha256: str = UNKNOWN,
                     variant: str = UNKNOWN) -> "Producer":
        """调用方**确定**这份 md 就是该后端自己产出的,才用这个入口。"""
        return cls(name=backend or UNKNOWN, source=source, source_sha256=source_sha256,
                   variant=variant)



@dataclass(frozen=True)
class Provenance:
    """一个块的来源:哪个后端、哪一段、段内第几块、落在第几页。"""

    backend: str
    part_id: str
    block_index: int
    page_idx: int | None = None
    page_idx_source: str = "none"
    raw_type: str = ""
    bbox: tuple[float, float, float, float] | None = None
    bbox_space: str = ""
    text_level: int | None = None
    offset: int | None = None

    def __post_init__(self) -> None:
        if not self.backend:
            raise IRError("prov.backend 不能为空(不知道该块来自哪个后端)")
        if not self.part_id:
            raise IRError("prov.part_id 不能为空(不知道该块来自哪一段)")
        if not isinstance(self.block_index, int) or self.block_index < 0:
            raise IRError(f"prov.block_index 必须是 >=0 的整数: {self.block_index!r}")
        if self.page_idx is not None and (not isinstance(self.page_idx, int) or self.page_idx < 0):
            raise IRError(f"prov.page_idx 必须是 >=0 的整数或 None: {self.page_idx!r}")
        source = self.page_idx_source or "none"
        if source not in PAGE_SOURCES:
            raise IRError(f"未知的 page_idx_source: {source!r}")
        object.__setattr__(self, "page_idx_source", source)
        if self.page_idx is None and source != "none":
            raise IRError(f"page_idx 为 None 时 page_idx_source 必须是 none: {source!r}")
        if self.page_idx is not None and source == "none":
            # 有页号却说不出凭什么 —— 不接受。宁可置空,不伪造来源。
            raise IRError("prov.page_idx 有值时必须给出 page_idx_source")
        space = self.bbox_space or ""
        if space and space not in BBOX_SPACES:
            raise IRError(f"未知的 bbox_space: {space!r}")
        if not space:
            # 只有 bbox 没有坐标系 → 视为无 bbox(§E.3 坐标统一规则)
            object.__setattr__(self, "bbox", None)
        elif self.bbox is None:
            object.__setattr__(self, "bbox_space", "")
        else:
            box = tuple(float(v) for v in self.bbox)
            if len(box) != 4:
                raise IRError(f"bbox 必须是 4 个数: {self.bbox!r}")
            object.__setattr__(self, "bbox", box)   # type: ignore[arg-type]


@dataclass(frozen=True)
class Block:
    """一个 IR 块。``text`` 是**原文**,``md_span`` 是它在 book.md 中的区间。"""

    id: str
    kind: Kind
    text: str
    order: int
    prov: Provenance
    kind_confidence: Confidence
    editable: bool
    md_span: tuple[int, int] | None = None
    level: int | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise IRError("Block.id 不能为空")
        if self.kind not in KINDS:
            raise IRError(f"未知的 kind: {self.kind!r}")
        if self.kind_confidence not in CONFIDENCES:
            raise IRError(f"未知的 kind_confidence: {self.kind_confidence!r}")
        if self.order < 0:
            raise IRError(f"Block.order 必须 >=0: {self.order}")
        if self.editable:
            if self.md_span is None:
                raise IRError(f"可编辑块 {self.id} 必须有 md_span")
            start, end = self.md_span
            if start < 0 or end < start:
                raise IRError(f"块 {self.id} 的 md_span 非法: {self.md_span}")
            if end - start != len(self.text):
                raise IRError(
                    f"块 {self.id} 的 text({len(self.text)} 字符)与 md_span"
                    f"({end - start} 字符)不一致 —— 解析期不得规整正文"
                )
        elif self.md_span is not None:
            raise IRError(f"证据块 {self.id} 不得持有 md_span")
        if self.level is not None and not (1 <= self.level <= 6):
            raise IRError(f"块 {self.id} 的 level 越界: {self.level}")

    # --- 便捷视图 ---
    @property
    def sha256(self) -> str:
        return text_sha256(self.text)

    @property
    def is_evidence(self) -> bool:
        return not self.editable

    @property
    def page_idx(self) -> int | None:
        return self.prov.page_idx

    def with_page(self, page_idx: int | None, source: str) -> "Block":
        """换页归属(对齐阶段用);其余字段原样保留。"""
        if page_idx is None:
            prov = replace(self.prov, page_idx=None, page_idx_source="none")
        else:
            prov = replace(self.prov, page_idx=page_idx, page_idx_source=source)
        return replace(self, prov=prov)


@dataclass(frozen=True)
class Link:
    """Layer1 ↔ Layer2 的对齐关系。``confidence`` 说明这条关系有多硬。"""

    block_id: str
    evidence_id: str
    confidence: Confidence
    how: str = "anchor"

    def __post_init__(self) -> None:
        if not self.block_id or not self.evidence_id:
            raise IRError("Link 两端都必须有 id")
        if self.confidence not in CONFIDENCES:
            raise IRError(f"未知的 link confidence: {self.confidence!r}")


@dataclass(frozen=True)
class DocMeta:
    """整本文档的元信息。``prefix`` 是文首空白(往返逐字节一致需要它)。"""

    source: str
    backend: str
    total_pages: int
    params_fp: str
    created_at: int
    schema: int = IR_SCHEMA
    variant: str = "original"
    backend_evidence: dict = field(default_factory=dict)
    prefix: str = ""
    counts: dict = field(default_factory=dict)
    producer: Producer = field(default_factory=Producer)

    def __post_init__(self) -> None:
        if self.total_pages < 0:
            raise IRError(f"total_pages 不能为负: {self.total_pages}")
        if self.schema != IR_SCHEMA:
            raise IRError(f"IR schema 版本不支持: {self.schema}(本实现只认 {IR_SCHEMA})")


@dataclass(frozen=True)
class BookIR:
    """一本书的 IR:元信息 + 两层块 + 对齐关系 + 页集合(1..N)。"""

    meta: DocMeta
    blocks: tuple[Block, ...]
    links: tuple[Link, ...]
    pages: tuple[int, ...]

    # --- 分层视图 ---
    @property
    def editable_blocks(self) -> tuple[Block, ...]:
        return tuple(b for b in self.blocks if b.editable)

    @property
    def evidence_blocks(self) -> tuple[Block, ...]:
        return tuple(b for b in self.blocks if not b.editable)

    def block(self, block_id: str) -> Block | None:
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None

    # --- 页归属统计(阶段 P5 的校验要读它) ---
    @property
    def page_attribution_missing(self) -> int:
        """没有页归属的可编辑/图片块数量。

        证据块不需要页归属吗?需要,但它由后端直接给出,拿不到就是后端没给,
        计在这里没有意义 —— 所以只统计 Layer1。
        """
        return sum(1 for b in self.blocks if b.editable and b.page_idx is None)

    @property
    def page_attribution_required(self) -> int:
        return sum(1 for b in self.blocks if b.editable)

    def verify_page_attribution(self) -> None:
        """严格模式:任何可编辑块没有页归属即判 IR 构造失败(§E.5 不变量 7)。

        P1 默认不调用它(阶段 P5 打开):真实书上的对齐失败必须先能被观测到,
        再决定是修对齐还是收紧闸门。
        """
        missing = [b.id for b in self.blocks if b.editable and b.page_idx is None]
        if missing:
            raise IRError(
                f"{len(missing)} 个可编辑块没有页归属(例: {', '.join(missing[:5])});"
                f"按 v2 契约不得估值,应记为 content_page_attribution_missing"
            )

    # --- 改写入口(唯一允许改文本的地方) ---
    def rewrite(self, block_id: str, new_text: str) -> "BookIR":
        """替换某个**可编辑块**的正文并返回新 IR;证据块抛 ``ReadonlyBlockError``。"""
        target = self.block(block_id)
        if target is None:
            raise IRError(f"没有这个块: {block_id}")
        if not target.editable:
            raise ReadonlyBlockError(block_id, target.kind)
        start, _ = target.md_span or (0, 0)
        updated = replace(target, text=new_text, md_span=(start, start + len(new_text)))
        blocks = tuple(updated if b.id == block_id else b for b in self.blocks)
        return replace(self, blocks=blocks)

    def replace_block(self, block_id: str, new_block: Block) -> "BookIR":
        """整块替换(改层级等非正文改动走这里);id 必须不变。"""
        if new_block.id != block_id:
            raise IRError(f"replace_block 不允许改 id: {block_id} → {new_block.id}")
        if self.block(block_id) is None:
            raise IRError(f"没有这个块: {block_id}")
        blocks = tuple(new_block if b.id == block_id else b for b in self.blocks)
        return replace(self, blocks=blocks)

    def remove_block(self, block_id: str) -> "BookIR":
        """删掉一个块**连同它的 trailer**,并清掉指向它的 link。

        链接必须一起清:留着悬空 link,``validate()`` 会立刻拒绝这份 IR,
        而"删块时顺手删链接"比"让下游各自过滤"更难忘。
        """
        if self.block(block_id) is None:
            raise IRError(f"没有这个块: {block_id}")
        blocks = tuple(b for b in self.blocks if b.id != block_id)
        links = tuple(l for l in self.links
                      if l.block_id != block_id and l.evidence_id != block_id)
        return replace(self, blocks=blocks, links=links)

    def insert_block(self, block: Block) -> "BookIR":
        """按 ``(层, order)`` 把块插回去(撤销删除用);同 order 时排在同层之后。"""
        if self.block(block.id) is not None:
            raise IRError(f"块已存在,拒绝重复插入: {block.id}")
        blocks = tuple(sorted(self.blocks + (block,), key=lambda b: (not b.editable, b.order)))
        return replace(self, blocks=blocks)

    # --- 一致性自检 ---
    def validate(self) -> None:
        """结构性自检:block/link 互指、order 单调、页集合与 total_pages 一致。"""
        ids = [b.id for b in self.blocks]
        if len(set(ids)) != len(ids):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise IRError(f"块 id 重复: {dup[:5]}")
        known = set(ids)
        for link in self.links:
            if link.block_id not in known or link.evidence_id not in known:
                raise IRError(f"link 指向不存在的块: {link}")
        for layer in (self.editable_blocks, self.evidence_blocks):
            orders = [b.order for b in layer]
            if orders != sorted(orders):
                raise IRError("同层块的 order 必须严格按阅读顺序递增")
        if self.pages and len(self.pages) != self.meta.total_pages:
            raise IRError(
                f"页集合 {len(self.pages)} 项与 total_pages {self.meta.total_pages} 不符"
            )

    def counts(self) -> dict:
        """稳定顺序的统计(写进 ``meta.json``)。"""
        return {
            "blocks": len(self.blocks),
            "editable": len(self.editable_blocks),
            "evidence": len(self.evidence_blocks),
            "links": len(self.links),
            "page_attribution_missing": self.page_attribution_missing,
        }


def iter_text(blocks: Iterable[Block]) -> str:
    """按 ``text + extra['trailer']`` 拼接(渲染与切片校验共用的唯一拼法)。"""
    return "".join(b.text + str(b.extra.get("trailer", "")) for b in blocks)
