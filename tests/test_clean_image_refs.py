"""清理阶段的图片引用存在性校验(P7 验收发现)。

验收里注入「正文引用 5 张图、work/images/ 整个目录不存在」时发现:清理报告
``issues`` 是空的,缺失要到 EPUB 阶段才被发现 —— 严重程度被倒置
(**缺一张会报,整批缺反而不报**),而且是静默的。

根因在 ``clean.rules.check_image_refs``:``images_dir`` 指向的目录不存在时直接
``return ()``,把「目录缺失」当成了「没有图片可查」。目录不在正是最强的缺失信号。

这里守三件事(前两件是这次修的,第三件是原有的契约,防止改回去):

1. 目录不存在 + 正文引用了相对路径图片 → 逐条报缺失;
2. 目录不存在 + 正文没有图片引用 → 不报(纯文本输入不能凭空多出告警);
3. ``images_dir=None`` → 跳过(纯文本输入没有 images/ 目录)。
"""
from __future__ import annotations

from pathlib import Path

from clean.pipeline import clean_text
from clean.rules import check_image_refs

#: 引用了图,但图片目录不存在(验收里复现的形态)
MD_WITH_IMAGES = (
    "# 图册\n"
    "\n"
    "![封面](images/cover.jpg)\n"
    "\n"
    "正文一段。\n"
    "\n"
    "![插图](images/fig2.png)\n"
)

#: 没有任何图片引用的纯文本
MD_PLAIN = "# 纯文本\n\n一段没有图片的文字。\n"


def test_missing_images_dir_reports_every_reference(tmp_path: Path) -> None:
    """images/ 整个目录不存在时,正文引用的每张图都要报缺失(而不是静默)。"""
    absent = tmp_path / "images"                 # 刻意不创建
    issues = check_image_refs(MD_WITH_IMAGES, absent)

    assert [i for i in issues if "cover.jpg" in i], f"cover.jpg 未报缺失: {issues}"
    assert [i for i in issues if "fig2.png" in i], f"fig2.png 未报缺失: {issues}"
    assert len(issues) == 2


def test_no_image_refs_means_no_issue_even_without_dir(tmp_path: Path) -> None:
    """没有图片引用的纯文本,目录不存在也不该报(不能凭空制造告警)。"""
    assert check_image_refs(MD_PLAIN, tmp_path / "images") == ()


def test_images_dir_none_is_skipped() -> None:
    """images_dir=None(纯文本输入,调用方没给目录)仍然跳过。"""
    assert check_image_refs(MD_WITH_IMAGES, None) == ()


def test_existing_images_are_not_reported(tmp_path: Path) -> None:
    """目录在且图片齐全时一条都不报(原有契约,防止改成一律报)。"""
    images = tmp_path / "images"
    images.mkdir()
    (images / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (images / "fig2.png").write_bytes(b"\x89PNG")
    assert check_image_refs(MD_WITH_IMAGES, images) == ()


def test_clean_text_surfaces_missing_dir_in_report_issues(tmp_path: Path) -> None:
    """走正式入口 clean_text:缺失要出现在 outcome.issues(报告 issues)里。

    这是这次验收真正断掉的一环 —— 之前 issues 为空,桌面端/日志都看不到。
    """
    outcome = clean_text(MD_WITH_IMAGES, backend="mineru",
                         source=str(tmp_path / "book.md"),
                         images_dir=tmp_path / "images")     # 目录不存在
    assert [i for i in outcome.issues if "图片引用缺失" in i], \
        f"清理报告 issues 里没有图片缺失: {outcome.issues}"
    # 只报结论,不动正文
    assert outcome.md_after == MD_WITH_IMAGES
