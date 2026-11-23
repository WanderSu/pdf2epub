"""标题裁决的结果必须落盘(写回判据不能用裁决之后的快照)。

标题裁决只改标题行、不产生 Patch。``clean_book_md`` 曾经按 ``outcome.changed``
(比较裁决**之后**的 before/after)决定要不要写回:只补/降标题时两者相等,于是
裁决结果留在内存里,产物里还是旧的标题层级 —— 报告写着「补回 N 个」,EPUB 的
导航却与目次对不上。
"""
from __future__ import annotations

BODY_A = "第一章正文,内容足够长,第一段前二十四个字要够长才能当锚点用。"
BODY_B = "第二章正文,同样足够长,第一段前二十四个字也要够长才行。"

#: 没有目录页、也没有任何规则会改字的稿子:唯一的改动来源就是标题裁决
MD = f"""\
# 书名页

{BODY_A}

{BODY_B}
"""

EVIDENCE = [
    {"type": "header", "text": "第一章 起头", "page_idx": 0, "bbox": [10.0, 20.0, 300.0, 40.0]},
    {"type": "text", "text": BODY_A, "page_idx": 0, "bbox": [10.0, 60.0, 300.0, 90.0]},
    {"type": "header", "text": "第二章 续篇", "page_idx": 1, "bbox": [10.0, 20.0, 300.0, 40.0]},
    {"type": "text", "text": BODY_B, "page_idx": 1, "bbox": [10.0, 60.0, 300.0, 90.0]},
]


def test_heading_only_change_is_written_back(tmp_path) -> None:
    """只有标题裁决、一条 Patch 都没有的一轮,结果也必须落盘。"""
    from pdf2epub.clean.pipeline import clean_book_md

    path = tmp_path / "book.md"
    path.write_text(MD, encoding="utf-8")
    outcome = clean_book_md(path, backend="mineru", evidence=EVIDENCE)

    assert outcome.applied == 0, "这个用例要的是「只有裁决、没有 Patch」的场景"
    assert outcome.md_before == outcome.md_after, "规则没改字:旧判据会因此不写回"
    on_disk = path.read_text(encoding="utf-8")
    assert on_disk == outcome.md_after
    assert "# 第二章 续篇" in on_disk, "裁决补回的标题没有落盘"
