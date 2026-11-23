"""结果包处理:白名单、结构化结果定位、跨段重名图片与页码注释。"""
from __future__ import annotations

from pathlib import Path

from pdf2epub.backends.mineru_package import (
    CONTENT_LIST_FILE,
    content_list_files,
    copy_part_images,
    part_marker,
    wanted_member,
)


def test_wanted_member_whitelist() -> None:
    assert wanted_member("full.md")
    assert wanted_member("1-200/full.md")
    assert wanted_member("images/a.png")
    assert wanted_member("1-200/images/a.png")
    assert wanted_member("1-200/1-200_content_list.json")
    assert wanted_member("x_content_list_v2.json")
    # 不在白名单:不解压,缩小攻击面
    assert not wanted_member("layout.json")
    assert not wanted_member("1-200/layout.json")
    assert not wanted_member("source.pdf")


def test_content_list_prefers_v1(tmp_path: Path) -> None:
    (tmp_path / "seg_content_list.json").write_text("[]", encoding="utf-8")
    (tmp_path / "seg_content_list_v2.json").write_text("{}", encoding="utf-8")
    files = content_list_files(tmp_path)
    assert files[CONTENT_LIST_FILE].name == "seg_content_list.json"


def test_content_list_absent_is_not_an_error(tmp_path: Path) -> None:
    assert content_list_files(tmp_path) == {}


def test_identical_image_is_reused_without_rename(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "fig.png").write_bytes(b"same")
    (dst / "fig.png").write_bytes(b"same")
    md, count = copy_part_images(src, dst, "![](images/fig.png)", prefix="p2_")
    assert md == "![](images/fig.png)"
    assert count == 1
    assert sorted(p.name for p in dst.iterdir()) == ["fig.png"]


def test_colliding_image_gets_prefix_and_md_updated(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "fig.png").write_bytes(b"second")
    (dst / "fig.png").write_bytes(b"first")
    md, count = copy_part_images(src, dst, "![](images/fig.png)", prefix="p2_")
    assert md == "![](images/p2_fig.png)"
    assert count == 1
    assert (dst / "fig.png").read_bytes() == b"first"      # 前一段的图没被覆盖
    assert (dst / "p2_fig.png").read_bytes() == b"second"


def test_part_marker_uses_real_page_range_or_falls_back() -> None:
    assert part_marker("201-302", 2).startswith("<!-- page ")
    assert "page-group 3" in part_marker(None, 3)
