"""ocr_spaces / cjk_spaces 迁移到 Patch 体系后的行为测试(P3)。

覆盖面(只测各规则实际适用的边界):
  - ocr_spaces:中文语境里被拆开的拉丁词合并(``Py Mu PDF``);正常英文短语
    不满足碎片条件时不合并;结构行(标题/目录/注释)不动;行内代码、代码块
    不动;重复执行幂等;
  - cjk_spaces:汉字-汉字、汉字-数字、数字-汉字、半角括号、内联 HTML 标签
    两侧的空格删除;中英之间保留;标题/目录/注释行不动;诗行不动;独立成段的
    版式行不动;行内代码不动;幂等;
  - 两者的新旧引擎差异测试(与 legacy 逐字节对比,已知的「文件末尾换行」
    分歧除外 —— 见 test_clean_rules_join 的显式断言)。

夹具全部合成,不含真实书籍正文。
"""
from __future__ import annotations

import difflib

from clean import apply_patches, propose_cjk_spaces, propose_ocr_spaces
from ir import build_ir, render_ir

# ---------------------------------------------------------------- 辅助


def _run(md: str, proposer, *, evidence: list | None = None):
    """提案 + 强制应用,返回 (渲染结果, 报告或 None)。"""
    ir = build_ir(md, source="synthetic.md", backend="synthetic",
                  evidence=evidence)
    proposal = proposer(ir)
    if not proposal.patches:
        return md, None
    result = apply_patches(ir, proposal.patches, allow_heuristic_apply=True,
                           started_at=0, finished_at=0)
    return render_ir(result.ir), result.report


def _legacy(md: str, *, rule: str) -> str:
    from legacy_snapshot import frozen

    return frozen(f"{rule}_only", md)


def _assert_matches_legacy(md: str, *, rule: str) -> str:
    out, _report = _run(md, propose_ocr_spaces if rule == "ocr_spaces"
                        else propose_cjk_spaces)
    legacy = _legacy(md, rule=rule)
    assert out.rstrip("\n") == legacy.rstrip("\n"), "".join(
        difflib.unified_diff(legacy.splitlines(keepends=True),
                             out.splitlines(keepends=True), "legacy", "ir"))
    return out


# ================================================================ ocr_spaces

def test_ocr_spaces_merges_split_latin_word_in_cjk_context() -> None:
    out, _ = _run("这份 PDF 由 Py Mu PDF 工具链提取而成。\n", propose_ocr_spaces)
    assert "PyMuPDF" in out and "Py Mu PDF" not in out


def test_ocr_spaces_leaves_normal_english_phrases_alone() -> None:
    md = "the cat sat on the mat beside 一只猫。\n"
    assert _run(md, propose_ocr_spaces)[0] == md


def test_ocr_spaces_needs_two_short_fragments() -> None:
    """≥3 个片段但短碎片(≤2 字母)不足 2 个 → 不合并(可能是真短语)。"""
    md = "这是 Unix like system 风格的写法说明。\n"
    assert _run(md, propose_ocr_spaces)[0] == md


def test_ocr_spaces_skips_structure_lines() -> None:
    md = ("# 第一章 Py Mu PDF 简介\n"
          "\n"
          "第一章 Py Mu PDF 简介/12\n"
          "\n"
          "① 见 Py Mu PDF 官方文档\n")
    assert _run(md, propose_ocr_spaces)[0] == md


def test_ocr_spaces_protects_inline_code_and_code_blocks() -> None:
    md = ("正文里写着 `Py Mu PDF 原样保留`,后面继续。\n"
          "\n"
          "```text\nPy Mu PDF\n```\n")
    out, _ = _run(md, propose_ocr_spaces)
    assert "`Py Mu PDF 原样保留`" in out
    assert "```text\nPy Mu PDF\n```" in out


def test_ocr_spaces_matches_legacy() -> None:
    _assert_matches_legacy(
        "这份 PDF 由 Py Mu PDF 工具链处理,输出 EP UB 格式。\n", rule="ocr_spaces")


def test_ocr_spaces_is_idempotent() -> None:
    md = "这份 PDF 由 Py Mu PDF 工具链提取而成。\n"
    once, _ = _run(md, propose_ocr_spaces)
    twice, report = _run(once, propose_ocr_spaces)
    assert twice == once
    assert report is None                       # 第二次零提案


# ================================================================ cjk_spaces

def test_cjk_spaces_removes_space_between_han_characters() -> None:
    out, _ = _run("中文 排版 空格应该被删除,这是规定。\n", propose_cjk_spaces)
    assert "中文排版空格应该被删除" in out


def test_cjk_spaces_removes_space_between_han_and_digit() -> None:
    out, _ = _run("全书共 12 章,第 3 章讲排版,印刷 3000 册。\n", propose_cjk_spaces)
    assert out == "全书共12章,第3章讲排版,印刷3000册。\n"


def test_cjk_spaces_keeps_space_between_latin_and_han() -> None:
    md = "用 Ubuntu 系统打开 PDF 文件。\n"
    assert _run(md, propose_cjk_spaces)[0] == md


def test_cjk_spaces_handles_parens_and_html_tags() -> None:
    out, _ = _run("书名 (第一版) 与 <u>下划线</u> 标记的说明。\n", propose_cjk_spaces)
    assert "(第一版)" in out
    assert "<u>下划线</u>标记" in out


def test_cjk_spaces_skips_headings_catalog_and_notes() -> None:
    md = ("# 第一章 巴勒斯坦问题的由来\n"
          "\n"
          "第一章 巴勒斯坦问题的由来/1\n"
          "\n"
          "① 列宁:《告犹太工人书》\n")
    assert _run(md, propose_cjk_spaces)[0] == md


def test_cjk_spaces_skips_verse_lines() -> None:
    md = "白日依山尽\n黄河入海流\n"
    out, _ = _run(md, propose_cjk_spaces)
    assert out == md


def test_cjk_spaces_skips_isolated_layout_lines() -> None:
    """版权页那种「一行一个字段」:短、无句末标点、独占一段 → 空格是结构。"""
    md = "人民出版社出版 新華書店发行\n"
    assert _run(md, propose_cjk_spaces)[0] == md


def test_cjk_spaces_protects_inline_code() -> None:
    md = "正文 `git 中文 命令` 原样保留,后面的 中文 空格要删。\n"
    out, _ = _run(md, propose_cjk_spaces)
    assert "`git 中文 命令`" in out
    assert "后面的中文空格要删" in out


def test_cjk_spaces_matches_legacy() -> None:
    _assert_matches_legacy(
        "中文 排版 空格 与 12 章 的处理,以及 (括号) 和 <u>标签</u> 一览。\n",
        rule="cjk_spaces")


def test_cjk_spaces_is_idempotent() -> None:
    md = "中文 排版 空格应该被删除。\n"
    once, _ = _run(md, propose_cjk_spaces)
    twice, report = _run(once, propose_cjk_spaces)
    assert twice == once
    assert report is None


def test_cjk_spaces_reports_per_block_patches() -> None:
    md = "第一段 中文 空格。\n\n第二段 数字 12 与 汉字。\n"
    ir = build_ir(md, backend="synthetic")
    proposal = propose_cjk_spaces(ir)
    assert len(proposal.patches) == 2
    assert all(p.operation == "replace_text" for p in proposal.patches)
    assert all(p.rule_id == "cjk_spaces" for p in proposal.patches)
