"""结构化脚注 → Pandoc footnote 重建(最小语义层)。

**解决什么**:OCR 的结构化结果里有脚注文本(`page_footnote` 块,带 `page_idx`/`bbox`),
正文里有引用标记(`①`/`②`……)。两者建立**可验证的对应关系**之后,才能生成真正的
Pandoc footnote(`正文[^1]` + `[^1]: 注释`),再由 pandoc 写成 EPUB 原生、可点击、
可返回的 footnote;而不是把注释当普通文本堆在页脚。

**为什么不能全文搜第一个 `①`**:同一本书里 `①` 每页都可能重复出现,
「全文第一个 ① ↔ 第一条脚注」这种匹配会系统性错位。这里只用**可验证的证据**:

1. 按 marker 分组(①组、②组……),组内两边的**数量必须相等** —— OCR 少认一个引用或
   一条脚注都会让数量不等,此时**整组不转**(顺序双射一旦错位,后面全是错的);
2. 组内按阅读顺序**一一对应**(引用按在 Markdown 里的出现顺序,脚注按
   `(page_idx, bbox 上沿)` 排序);
3. 每一对都要过**页窗校验**:脚注所在页必须等于引用所在页,或紧随其后
   (MinerU 的正文块可能跨页,块归属页取起始页,所以允许 +1 页);
4. 任何一对不过页窗 → **整组降级为不转**。

置信度:`同页` = high,`相邻页(+1)` = medium。**只有 high/medium 会被转换**;
LOW(证据不足、有多个候选、数量不等且找不到唯一候选)一律保留为普通文本 ——
「一个没配上的脚注保持普通文本」远好于「配错」。

**稳定编号**:`id` 由调用方跨分片递增(`next_id`),不按分片各自从 1 开始 ——
分片合并后 `[^1]` 不会撞车,resume 与重复运行结果一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

#: 圈码序号(中文书籍正文引用与页脚注释最常用的标记)
CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

#: 正文引用 marker:裸圈码,或 OCR 把它包成行内公式的形式(PaddleOCR 会输出 `$ ^{①} $`)
REF_RE = re.compile(rf"[{CIRCLED_DIGITS}]|\$\s*\^\{{[{CIRCLED_DIGITS}]\}}\s*\$")
#: 从匹配文本里取出那个圈码
_CIRCLED_IN_RE = re.compile(rf"[{CIRCLED_DIGITS}]")

#: 脚注块里 marker 之后的分隔符(要去掉,定义里不该再带一遍 marker)
MARKER_SEP_RE = re.compile(rf"^\s*[{CIRCLED_DIGITS}]+\s*[、.．,，:：]?\s*")

#: 与 Markdown 对齐时用的锚点长度(MinerU 恢复层同一套做法:去空白后取前缀)。
#: 用 24 字而不是 12:实际书里短锚点会撞上重复短语(页眉、口号、`4` 这种页码块),
#: 一旦某个块的锚点命中到错误位置,后续所有引用的页归属都会跟着错 —— 实测 12 字时
#: 10 个引用里 8 个页归属错误,24 字全部正确。
ANCHOR_CHARS = 24
#: 参与对齐的块至少要这么长(可见字符):更短的块(text 只有 `4`、`` 这类)锚点没有
#: 区分度,只会污染对齐。
MIN_ANCHOR_CHARS = 8

#: 结构化结果里代表「页脚脚注」的块类型
FOOTNOTE_BLOCK_TYPES = ("page_footnote",)

HIGH = "high"
MEDIUM = "medium"


@dataclass(frozen=True)
class StructuredFootnote:
    """来自结构化结果的一条脚注。"""

    marker: str                     # ① / ② …
    text: str                       # 原始文本(含 marker)
    page_idx: int | None = None
    bbox: Sequence[float] | None = None
    source: str = "structured"

    @property
    def body(self) -> str:
        """去掉开头的 marker 与分隔符后的正文。"""
        body = MARKER_SEP_RE.sub("", self.text).strip()
        return body or self.text.strip()

    @property
    def y(self) -> float:
        return float(self.bbox[1]) if self.bbox and len(self.bbox) >= 2 else 0.0


@dataclass(frozen=True)
class FootnoteRef:
    """正文里的一个引用标记。"""

    marker: str
    start: int                      # marker 在 Markdown 里的起始偏移
    end: int
    page_idx: int | None = None


@dataclass(frozen=True)
class FootnoteLink:
    """引用 ↔ 脚注 的对应关系。"""

    ref: FootnoteRef
    note: StructuredFootnote
    confidence: str
    id: int


@dataclass
class ReconstructResult:
    md: str
    links: list[FootnoteLink] = field(default_factory=list)
    unmatched: list[StructuredFootnote] = field(default_factory=list)
    orphan_refs: list[FootnoteRef] = field(default_factory=list)
    next_id: int = 1
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 结构化结果读取


def _block_text(block: dict) -> str:
    """取 content_list 块的纯文本:v1 是扁平 `text`,v2 是 `content.*_content[]`。"""
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    content = block.get("content")
    if isinstance(content, dict):
        parts: list[str] = []
        for value in content.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and isinstance(item.get("content"), str):
                        parts.append(item["content"])
        return "".join(parts).strip()
    return ""


def load_content_list(path) -> list[dict]:
    """读结构化结果(v1 平铺列表 / v2 按页分组两种形态都吃);坏了就当没有。"""
    import json
    from pathlib import Path

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    if data and all(isinstance(page, list) for page in data):      # v2:[[块…], …]
        return [block for page in data for block in page if isinstance(block, dict)]
    return [block for block in data if isinstance(block, dict)]


def structured_footnotes(blocks: Iterable[dict], *, source: str = "structured"
                         ) -> list[StructuredFootnote]:
    """从内容块里取出脚注(`page_footnote`),按阅读顺序排序。"""
    notes: list[StructuredFootnote] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in FOOTNOTE_BLOCK_TYPES:
            continue
        text = _block_text(block)
        if not text:
            continue
        found = _CIRCLED_IN_RE.search(text)
        if not found:                    # 没有圈码 marker 的注释(如 `* Corresponding author`)
            notes.append(StructuredFootnote(marker="", text=text,
                                            page_idx=block.get("page_idx"),
                                            bbox=block.get("bbox"), source=source))
            continue
        notes.append(StructuredFootnote(marker=found.group(0), text=text,
                                        page_idx=block.get("page_idx"),
                                        bbox=block.get("bbox"), source=source))
    notes.sort(key=lambda n: (n.page_idx if n.page_idx is not None else 1 << 30, n.y))
    return notes


# ---------------------------------------------------------------- 正文引用扫描


def _normalized(md: str) -> tuple[str, list[int]]:
    """去空白后的文本 + 「归一位置 → 原文位置」映射(对齐用)。"""
    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(md):
        if not ch.isspace():
            chars.append(ch)
            index_map.append(i)
    return "".join(chars), index_map


def _anchor_key(block: dict) -> str:
    """块的对齐锚点(去空白后的前 ``ANCHOR_CHARS`` 字);没有区分度的块返回空串。"""
    text = re.sub(r"\s+", "", _block_text(block))
    if len(text) < MIN_ANCHOR_CHARS:
        return ""
    return text[:ANCHOR_CHARS]


def block_page_spans(md: str, blocks: Sequence[dict]) -> list[tuple[int | None, int]]:
    """把内容块按阅读顺序与 Markdown 对齐,返回 [(page_idx, 块起始原文偏移)]。

    只用来给「正文引用」定页(脚注的 `page_idx` 由结构化结果直接给出)。要求:
      - 锚点足够长且有区分度(见 ``_anchor_key``,短块直接跳过);
      - 命中位置必须**单调递增**(块本身按阅读顺序给,不递增就是对齐错了);
      - 页号必须**非递减**(MinerU 的块按阅读顺序排列,页号倒退同样说明对齐错了)。
    对不齐只影响页归属精度:页窗不成立时整组不转,不会给出错误链接。
    """
    norm_md, index_map = _normalized(md)
    cursor = 0
    last_page: int | None = None
    spans: list[tuple[int | None, int]] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") in FOOTNOTE_BLOCK_TYPES:
            continue
        key = _anchor_key(block)
        if not key:
            continue
        pos = norm_md.find(key, cursor)
        if pos < 0:
            continue
        page = block.get("page_idx")
        if last_page is not None and isinstance(page, int) and page < last_page:
            continue                     # 页号倒退 → 对齐已失准,该块不参与定页
        cursor = pos + len(key)
        if isinstance(page, int):
            last_page = page
        spans.append((page, index_map[pos]))
    return spans


def _page_at(offset: int, spans: Sequence[tuple[int | None, int]]) -> int | None:
    page: int | None = None
    for page_idx, start in spans:
        if start <= offset:
            page = page_idx
        else:
            break
    return page


def scan_refs(md: str, spans: Sequence[tuple[int | None, int]] = ()) -> list[FootnoteRef]:
    """扫描正文引用标记。跳过行首标记(那是注释自己的 marker)与代码块里的字符。"""
    refs: list[FootnoteRef] = []
    fence_positions = [m.start() for m in re.finditer(r"^```", md, re.M)]
    for m in REF_RE.finditer(md):
        marker = _CIRCLED_IN_RE.search(m.group(0))
        if marker is None:
            continue
        line_start = md.rfind("\n", 0, m.start()) + 1
        if not md[line_start:m.start()].strip():        # 行首 → 注释行,不是引用
            continue
        if sum(1 for p in fence_positions if p < m.start()) % 2:   # 在围栏代码块里
            continue
        refs.append(FootnoteRef(marker=marker.group(0), start=m.start(), end=m.end(),
                                page_idx=_page_at(m.start(), spans)))
    return refs


# ---------------------------------------------------------------- 匹配


def _page_delta(note_page: int | None, ref_page: int | None) -> int | None:
    if note_page is None or ref_page is None:
        return None
    return note_page - ref_page


def match_footnotes(refs: Sequence[FootnoteRef], notes: Sequence[StructuredFootnote],
                    *, page_tol: int = 1) -> tuple[list[tuple[FootnoteRef, StructuredFootnote, str]],
                                                    list[StructuredFootnote],
                                                    list[FootnoteRef], dict]:
    """建立引用 ↔ 脚注的对应关系。返回 (配对, 未匹配脚注, 孤立引用, 统计)。

    规则见模块 docstring:**按 marker 分组 + 数量必须相等 + 顺序双射 + 页窗校验**;
    任何一对不过页窗,整组不转。数量不等时只接受「同页唯一」/「相邻页唯一」的候选。
    """
    links: list[tuple[FootnoteRef, StructuredFootnote, str]] = []
    unmatched: list[StructuredFootnote] = []
    used_refs: set[int] = set()
    stats = {"refs": len(refs), "notes": len(notes),
             "groups_refused": 0, "high": 0, "medium": 0}

    by_marker: dict[str, list[StructuredFootnote]] = {}
    for note in notes:
        by_marker.setdefault(note.marker, []).append(note)

    for marker, group in by_marker.items():
        if not marker:                       # 没有圈码 marker 的注释:不自动配对
            unmatched.extend(group)
            continue
        cn = [i for i, r in enumerate(refs) if r.marker == marker]
        if len(cn) == len(group) and cn:
            pairs = [(refs[i], note) for i, note in zip(cn, group)]
            deltas = [_page_delta(note.page_idx, ref.page_idx) for ref, note in pairs]
            if all(d is not None and 0 <= d <= page_tol for d in deltas):
                for idx, ((ref, note), d) in zip(cn, zip(pairs, deltas)):
                    conf = HIGH if d == 0 else MEDIUM
                    stats[conf] += 1
                    used_refs.add(idx)
                    links.append((ref, note, conf))
                continue
            stats["groups_refused"] += 1
            unmatched.extend(group)          # 整组不转(顺序一旦错位,后面全是错的)
            continue
        for note in group:
            same = [i for i in cn if i not in used_refs and note.page_idx is not None
                    and refs[i].page_idx == note.page_idx]
            near = [i for i in cn if i not in used_refs and note.page_idx is not None
                    and (d := _page_delta(note.page_idx, refs[i].page_idx)) is not None
                    and 0 < d <= page_tol]
            if len(same) == 1:
                pick, conf = same[0], HIGH
            elif len(near) == 1:
                pick, conf = near[0], MEDIUM
            else:
                unmatched.append(note)       # 0 个候选(证据不足)或 ≥2 个(有歧义)
                continue
            stats[conf] += 1
            used_refs.add(pick)
            links.append((refs[pick], note, conf))

    links.sort(key=lambda item: item[0].start)
    orphan = [r for i, r in enumerate(refs) if i not in used_refs]
    return links, unmatched, orphan, stats


# ---------------------------------------------------------------- 渲染成 Pandoc footnote


def _paragraph_end(md: str, index: int) -> int:
    """index 所在段落的结束位置(下一个空行的起始处;没有则是正文末尾)。空行兼容 CRLF。"""
    match = re.search(r"\r?\n[ \t]*\r?\n", md[index:])
    return index + match.start() if match else len(md.rstrip())


def reconstruct_footnotes(md_text: str, blocks: Sequence[dict], *, next_id: int = 1,
                          page_tol: int = 1, source: str = "structured") -> ReconstructResult:
    """把结构化脚注与正文引用配对,生成 Pandoc footnote 形式的 Markdown。

    - 配对成功:正文 marker → `[^id]`,该页脚注位置 → `[^id]: 注释正文`;
    - 配对不成立(证据不足/有歧义):**脚注仍以普通文本插在原位置**(不丢内容),
      正文 marker 保持原样(不误链);
    - `id` 跨分片递增(`next_id`),保证分片合并后不撞号。
    """
    notes = structured_footnotes(blocks, source=source)
    if not notes:
        return ReconstructResult(md=md_text, next_id=next_id,
                                 stats={"refs": 0, "notes": 0, "linked": 0,
                                        "high": 0, "medium": 0, "unmatched": 0,
                                        "orphan_refs": 0})
    spans = block_page_spans(md_text, blocks)
    refs = scan_refs(md_text, spans)
    pairs, unmatched, orphan, base = match_footnotes(refs, notes, page_tol=page_tol)

    links: list[FootnoteLink] = []
    note_id: dict[int, int] = {}                 # id(note) → 全书编号
    for ref, note, conf in pairs:
        note_id[id(note)] = next_id
        links.append(FootnoteLink(ref=ref, note=note, confidence=conf, id=next_id))
        next_id += 1

    # 渲染:替换正文 marker + 在原页位置写脚注定义/兜底文本(一次排序后统一改文本)
    anchors = _page_anchors(md_text, blocks, notes)
    edits: list[tuple[int, int, int, str]] = []      # (起, 止, 顺序, 文本)
    for link in links:
        edits.append((link.ref.start, link.ref.end, -1, f"[^{link.id}]"))
    fallback = [n for n in notes if id(n) not in note_id]
    for order, note in enumerate(notes):
        pos = anchors.get((note.page_idx, note.marker, note.text), len(md_text))
        # 配对上的:写成 Pandoc 脚注定义(编号由 pandoc 渲染,所以去掉原 marker);
        # 没配上的:**原文照旧落回原位置**(连 marker 一起),不做任何改写。
        text = f"[^{note_id[id(note)]}]: {note.body}" if id(note) in note_id else note.text
        edits.append((pos, pos, order, "\n\n" + text))

    out = md_text
    for start, end, _order, text in sorted(edits, key=lambda e: (e[0], e[2]), reverse=True):
        out = f"{out[:start]}{text}{out[end:]}"

    stats = {**base, "linked": len(links), "unmatched": len(fallback),
             "orphan_refs": len(orphan)}
    return ReconstructResult(md=out, links=links, unmatched=fallback, orphan_refs=orphan,
                             next_id=next_id, stats=stats)


def _page_anchors(md: str, blocks: Sequence[dict], notes: Sequence[StructuredFootnote]
                  ) -> dict[tuple, int]:
    """每条脚注要插回的位置:它所在页最后一块内容的段落末尾(对不齐则文末)。

    与 ``block_page_spans`` 用同一套对齐规则(同样的锚点门槛与页号单调检查),
    这样「引用定页」和「脚注落位」不会各错一处。
    """
    norm_md, index_map = _normalized(md)
    cursor = 0
    last_page: int | None = None
    spans: dict[object, int] = {}
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") in FOOTNOTE_BLOCK_TYPES:
            continue
        key = _anchor_key(block)
        if not key:
            continue
        pos = norm_md.find(key, cursor)
        if pos < 0:
            continue
        page = block.get("page_idx")
        if last_page is not None and isinstance(page, int) and page < last_page:
            continue
        cursor = pos + len(key)
        if isinstance(page, int):
            last_page = page
        spans[page] = _paragraph_end(md, index_map[cursor - 1])
    anchors: dict[tuple, int] = {}
    for note in notes:
        anchors[(note.page_idx, note.marker, note.text)] = spans.get(note.page_idx, len(md))
    return anchors
