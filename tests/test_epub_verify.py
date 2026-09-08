"""EPUB 结构校验(计划 1.1):先写失败用例,再抽 src/epub/verify.py。

覆盖:
  - 正常 EPUB 无 error
  - 图片被删 → image_missing(消息含「图片引用缺失」)
  - 期望公式/脚注/图片数量不满足 → 对应 error
  - zip 损坏 / 容器缺失 → readable=False 且给出归档级 error
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from conftest import build_epub, rewrite_epub

from epub.verify import verify_epub


def test_valid_epub_has_no_errors(sample_epub: Path) -> None:
    r = verify_epub(sample_epub, expect_math=True, expect_images=1)
    assert r.readable
    assert r.errors == [], [i.message for i in r.errors]
    assert r.stats["math"] >= 1
    assert r.stats["img_refs"] >= 1
    assert r.stats["css"] >= 1
    assert r.stats["toc_links"] >= 1
    assert r.summary() == "0 失败 0 警告" or "0 失败" in r.summary()


def test_missing_image_is_reported(sample_epub: Path, tmp_path: Path) -> None:
    # 删掉包内图片(引用仍在),模拟「图片没被打进 EPUB」
    broken = rewrite_epub(sample_epub, tmp_path / "broken.epub", drop_suffixes=(".png", ".jpg"))
    r = verify_epub(broken)
    codes = [i.code for i in r.errors]
    assert "image_missing" in codes
    assert any("图片引用缺失" in i.message for i in r.errors)


def test_manifest_missing_file_is_reported(sample_epub: Path, tmp_path: Path) -> None:
    """manifest 列出但包内不存在 → manifest_missing(硬失败)。"""
    broken = rewrite_epub(sample_epub, tmp_path / "no_media.epub", drop_suffixes=(".png",))
    r = verify_epub(broken)
    assert any(i.code in ("manifest_missing", "image_missing") for i in r.errors)


def test_expect_math_fails_when_absent(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "w", tmp_path / "nofx.epub", with_math=False, with_image=False)
    assert verify_epub(epub).ok
    r = verify_epub(epub, expect_math=True)
    assert [i.code for i in r.errors] == ["math_expected"]


def test_expect_images_fails_when_fewer(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "w", tmp_path / "noimg.epub", with_image=False, with_math=False)
    r = verify_epub(epub, expect_images=2)
    assert "images_expected" in [i.code for i in r.errors]


def test_broken_zip_is_unreadable(tmp_path: Path) -> None:
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not a zip file")
    r = verify_epub(bad)
    assert r.readable is False
    assert r.ok is False
    assert r.errors[0].code == "archive_unreadable"


def test_container_missing(tmp_path: Path, sample_epub: Path) -> None:
    broken = rewrite_epub(sample_epub, tmp_path / "nocontainer.epub",
                          drop={"META-INF/container.xml"})
    r = verify_epub(broken)
    assert "container_missing" in [i.code for i in r.errors]


def test_css_missing(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "w", tmp_path / "nocss.epub", with_image=False, with_math=False)
    broken = rewrite_epub(epub, tmp_path / "nocss2.epub", drop_suffixes=(".css",))
    r = verify_epub(broken)
    assert "css_missing" in [i.code for i in r.errors]


def test_stats_counts_math(sample_epub: Path) -> None:
    with zipfile.ZipFile(sample_epub) as zf:
        names = zf.namelist()
    r = verify_epub(sample_epub)
    assert r.stats["entries"] == len(names)
    assert r.stats["chapters"] >= 1
