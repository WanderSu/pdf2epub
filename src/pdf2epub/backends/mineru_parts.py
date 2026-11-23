"""MinerU 分段计划:页码区间与段标识。

段标识(`part_id` / `page_range`)必须**跨批次稳定** —— 补提交某一段时它的缓存目录
与合并位置都不能变,「只补缺失段、不重复计费」才成立。因此这里只做纯计算,
不发请求、不碰磁盘。
"""
from __future__ import annotations

from .base import OcrPart

#: MinerU 官方单任务页数上限(超过按 page_ranges 分片)
MAX_PAGES_PER_TASK = 200


def build_page_ranges(total_pages: int, max_pages_per_task: int = MAX_PAGES_PER_TASK) -> list[str]:
    """按每任务页数上限生成 1-indexed 页码范围。

    302 页 → ``["1-200", "201-302"]``;不超过上限时返回 ``[]``(表示无需分片)。
    """
    limit = max(int(max_pages_per_task), 1)
    if total_pages <= limit:
        return []
    ranges: list[str] = []
    start = 1
    while start <= total_pages:
        end = min(start + limit - 1, total_pages)
        ranges.append(f"{start}-{end}")
        start = end + 1
    return ranges


def plan_parts(total_pages: int, max_pages_per_task: int = MAX_PAGES_PER_TASK) -> list[OcrPart]:
    """本文件会按哪些段提交;整本一段时不带 ``page_range``。"""
    ranges = build_page_ranges(total_pages, max_pages_per_task) or [f"1-{total_pages}"]
    sharded = len(ranges) > 1
    return [
        OcrPart(idx=i, part_id=rng, page_range=rng if sharded else None)
        for i, rng in enumerate(ranges, start=1)
    ]


def target_name(part: OcrPart, pdf_name: str, sharded: bool) -> str:
    """轮询匹配键:分片用 ``data_id``(part-N),整本单任务用文件名(与提交接口一致)。"""
    return f"part-{part.idx}" if sharded else pdf_name
