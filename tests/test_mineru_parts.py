"""分段计划的纯计算:页码区间与段标识(不发请求、不碰磁盘)。"""
from __future__ import annotations

from pdf2epub.backends.mineru_parts import build_page_ranges, plan_parts, target_name


def test_no_sharding_when_within_limit() -> None:
    assert build_page_ranges(200, 200) == []
    assert build_page_ranges(1, 200) == []
    assert len(plan_parts(3, 200)) == 1
    assert plan_parts(3, 200)[0].page_range is None      # 整本单任务不带 page_ranges


def test_ranges_are_one_indexed_and_cover_every_page() -> None:
    assert build_page_ranges(302, 200) == ["1-200", "201-302"]
    assert build_page_ranges(201, 200) == ["1-200", "201-201"]
    assert build_page_ranges(600, 200) == ["1-200", "201-400", "401-600"]


def test_parts_carry_stable_ids() -> None:
    parts = plan_parts(302, 200)
    assert [p.part_id for p in parts] == ["1-200", "201-302"]
    assert [p.idx for p in parts] == [1, 2]
    assert [p.page_range for p in parts] == ["1-200", "201-302"]


def test_limit_is_floored_at_one_page() -> None:
    # 配置写错(0 / 负数)时不能让循环原地打转
    assert build_page_ranges(3, 0) == ["1-1", "2-2", "3-3"]


def test_target_name_matches_submit_payload() -> None:
    parts = plan_parts(302, 200)
    assert target_name(parts[1], "书.pdf", sharded=True) == "part-2"
    assert target_name(parts[0], "书.pdf", sharded=False) == "书.pdf"
