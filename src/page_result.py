"""按页(或一段页)承载转换结果 —— hybrid 页序正确性的数据基础(P0-1)。

背景:hybrid 流程原先把「全部文字页一块 Markdown + 全部扫描页一块 Markdown」
按各组首页页码排序后拼接。text→scan→text 交错时扫描块会被整块提到最前,
页序错位、页边界丢失 —— 成品是「看着正常、内容其实错序」的坏 EPUB。

现在统一为页单元(PageResult):

- 文字页:PyMuPDF4LLM 逐页提取(``page_chunks=True``)→ 一页一个单元
- 扫描页:按**连续区段**(contiguous run)分组送 OCR → 一段一个单元

单元覆盖的页码用 ``pages``(1-indexed 升序)承载,因此即使区段被合并成
非连续页集合(见 ``merge_runs``),页码标注依然如实。合并时写入页码注释
(``<!-- page 3 -->`` / ``<!-- page 4-5 ocr -->`` / ``<!-- page 4,6 ocr -->``):
页序可被回归测试断言,人工也能在 book.md 里按页定位问题。

注意:除注释里提到的轻量依赖外不要在这里导入 pymupdf 等重依赖 ——
预检(dryrun)要用同一份区段规划,而预检必须保持轻量、只读。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

#: 扫描页连续区段数上限:超出则合并区段,避免云端 OCR 任务数失控
#: (每个区段 = 一次云端提交;交错严重的书可能切出几十个区段)
DEFAULT_MAX_OCR_RUNS = 8

#: 页码注释:由 page_marker() 生成,page_marks() 解析
PAGE_MARK_RE = re.compile(
    r"<!--\s*page\s+(?P<pages>[\d,\s-]+?)(?:\s+(?P<note>[A-Za-z_]+))?\s*-->"
)


def format_pages(pages: Iterable[int]) -> str:
    """页码列表 → 紧凑标注:``[4,5]`` → ``"4-5"``;``[4,6]`` → ``"4,6"``。"""
    nums = sorted({int(p) for p in pages})
    if not nums:
        return ""
    parts: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def parse_pages(text: str) -> list[int]:
    """``"4-5,7"`` → ``[4, 5, 7]``(与 format_pages 互逆)。"""
    out: list[int] = []
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, tail = chunk.partition("-")
        if not head.strip().isdigit():
            continue
        first = int(head)
        last = int(tail) if tail.strip().isdigit() else first
        out.extend(range(first, max(first, last) + 1))
    return out


@dataclass
class PageResult:
    """一页(或一段连续页)的转换结果。

    ``pages`` 是**原始 PDF 的 1-indexed 页码**(升序;非空),排序、页码注释
    与问题定位都以它为准 —— 不是区段内序号。
    """

    pages: tuple[int, ...]
    source: str                  # text(本地文字层)/ ocr
    markdown: str
    backend: str = ""

    @property
    def page_no(self) -> int:
        """首页页码(排序键)。"""
        return self.pages[0] if self.pages else 0

    @property
    def last_page_no(self) -> int:
        return self.pages[-1] if self.pages else 0

    @property
    def page_span(self) -> int:
        """覆盖的页码范围长度(合并区段可能大于实际页数)。"""
        return self.last_page_no - self.page_no + 1 if self.pages else 0

    @property
    def is_empty(self) -> bool:
        return not self.markdown.strip()


def page_marker(pages: Iterable[int], note: str | None = None) -> str:
    """页码注释,如 ``<!-- page 7 -->`` / ``<!-- page 4-5 ocr -->``。"""
    label = format_pages(pages)
    return f"<!-- page {label} {note} -->" if note else f"<!-- page {label} -->"


def page_marker_from_range(page_range: str, note: str | None = None) -> str:
    """把 ``"201-302"`` 形式的页码区间(云端 page_ranges)转成页码注释。

    无法解析时退化为 ``<!-- page-group ... -->``(旧格式),不静默丢标记。
    """
    pages = parse_pages(page_range)
    if not pages:
        return f"<!-- page-group {page_range} -->"
    return page_marker(pages, note)


def page_marks(md: str) -> list[list[int]]:
    """取出 Markdown 里的页码注释,每个注释展开成页码列表(按出现顺序)。"""
    return [parse_pages(m.group("pages")) for m in PAGE_MARK_RE.finditer(md)]


def contiguous_runs(page_idxs: Iterable[int]) -> list[list[int]]:
    """把页号(0-indexed,可乱序)切成连续区段:``[0,1,2,4,5]`` → ``[[0,1,2],[4,5]]``。"""
    runs: list[list[int]] = []
    for idx in sorted({int(i) for i in page_idxs}):
        if runs and idx == runs[-1][-1] + 1:
            runs[-1].append(idx)
        else:
            runs.append([idx])
    return runs


def merge_runs(runs: list[list[int]], limit: int) -> list[list[int]]:
    """把区段合并到 limit 段以内(按顺序均分,组内相邻区段合并)。

    合并后的区段是**非连续页集合**(只含扫描页,不含中间的文字页 —— 那些文字页
    仍由本地逐页提取,内容不重复)。代价是合并区段会整块排在自己的首页位置,
    中间的本地文字页因此被推到它后面:**局部页序**与原文不一致,内容不丢。
    调用方必须把这个代价作为警告打出来,不做静默处理。

    均分(而不是按间隔贪心)是为了让各任务页数接近、行为可预测。
    """
    runs = [list(r) for r in runs]
    n = len(runs)
    if limit <= 0 or n <= limit:
        return runs
    return [
        [p for r in runs[i * n // limit : (i + 1) * n // limit] for p in r]
        for i in range(limit)
    ]


def resolve_ocr_run_limit(
    value: object, default: int = DEFAULT_MAX_OCR_RUNS
) -> int:
    """把配置里的区段上限解析为正整数(缺失 / 非法 / 非正 → 默认值)。"""
    try:
        limit = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return limit if limit > 0 else default


def plan_ocr_runs(
    page_idxs: Iterable[int], limit: int = DEFAULT_MAX_OCR_RUNS
) -> list[list[int]]:
    """扫描页 → 实际会提交的 OCR 区段。

    预检(dryrun)与实际转换共用这一份规划,避免「预检说 1 个任务、实际提 5 个」。
    """
    runs = contiguous_runs(page_idxs)
    limit = resolve_ocr_run_limit(limit)
    if len(runs) > limit:
        runs = merge_runs(runs, limit)
    return runs


def merge_page_results(results: Iterable[PageResult], ocr_note: str = "ocr") -> str:
    """按页序合并页单元,写入页码注释(空单元跳过)。"""
    ordered = sorted((r for r in results if not r.is_empty), key=lambda r: r.page_no)
    parts = [
        f"{page_marker(r.pages, ocr_note if r.source == 'ocr' else None)}\n"
        f"{r.markdown.strip()}"
        for r in ordered
    ]
    return "\n\n".join(parts)
