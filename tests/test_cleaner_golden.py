"""清理器 golden 样本回归(v0.3.2 P0-1 / P6 起跑 IR 引擎)。

一个样本 = 一对文件:

    tests/fixtures/cleaner/<name>.md              输入
    tests/fixtures/cleaner/<name>.expected.md     期望输出(逐字节比对,只用 LF)
    tests/fixtures/cleaner/<name>.opts.json       可选:{"disable": [...], "images": [...],
                                                        "expect_issues": ["图片引用缺失"]}
    tests/fixtures/cleaner/<name>.evidence.json   可选:结构证据层(content_list)

约定:

- 每条规则都要有「该改的」与「不该改的」两类样本(见 ``GOLDEN_MAP``),误伤比漏改严重。
- 新增样本时,**期望输出按「你希望的结果」写**,再跑测试;实际不一致时先判断是代码错
  还是期望写错,不要为了让测试变绿去改期望。改阈值/改行为必须同时改样本并说明原因。
- 期望文件逐字节比对(**含结尾换行**):旧引擎会把文件末尾换行丢掉,P6 的 IR 引擎
  保住了它(`render(parse(md)) == md` 的往返契约要求这样),所以期望文件一律以
  换行结尾,不再兼容「少一个换行」的结果。

P6 的两处行为变化(样本已同步):

- **跨页拼接需要结构证据**:没有 content_list 的输入(手写 md)里,跨块的拼接只产
  建议(``suggested``)、正文不动 —— 带 LLC 的流程要验这段,请给 ``*.evidence.json``
  (`page_markers` 就是这么做的)。
- **合并后的段落留在它真正起头的那一侧**:跨页码注释拼接时,注释保持原位不动,
  落句的那一块留在注释**之前**(它本来就属于上一页);旧引擎把它搬到注释之后,
  于是产品谎报"这段从第 21 页开始"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clean import clean_text
from clean.options import CLEAN_KEYS, CleanOptions
from clean.policy import NON_PATCH_RULES, PIPELINE_ORDER
from ir import Producer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cleaner"

#: 规则 → 覆盖它的样本(至少一个正向样本;含反例的样本会同时列在这里)
GOLDEN_MAP: dict[str, list[str]] = {
    "page_numbers": ["page_numbers", "code_fence_protected"],
    #   正例:独立页码行被删;反例:年份 1984、句内数字保留、代码块里的 123/32 保留
    "running_heads": ["running_heads", "code_fence_protected"],
    #   正例:重复 3 次被删;反例:只出现 2 次的短行保留、代码块里重复的 end 保留
    "join_lines": ["join_lines", "poetry_preserved", "poetry_quoted", "poetry_modern",
                   "page_markers", "ordered_list_toc", "footnotes", "structure_lines"],
    #   正例:断行拼接(含「短行散文」那对)、跨页码注释的拼接(page_markers,需证据层)
    #   反例:代码块/表格不拼、诗行不拼且加硬换行、标题/列表/引用跨注释不拼
    #   (page_markers)、有序列表条目与页码行不被粘进相邻行(ordered_list_toc 取自
    #   真实书籍目录)。
    #   (hard break 也挂在 join_lines 开关下:它是「怎么处理换行」的同一件事)
    #   poetry_modern:现代诗行 —— 曾经只判「不拼接」而不加硬换行。
    #   footnotes:脚注定义块整体受保护 —— 反例里正文行不以句末标点结尾、脚注定义紧跟其后。
    "ocr_spaces": ["ocr_spaces", "code_fence_protected"],
    "cjk_spaces": ["cjk_spaces", "should_not_touch", "code_fence_protected", "page_markers",
                   "structure_lines"],
    "dup_headings": ["dup_headings"],              # 正例:相邻同名同级去重;反例:被正文隔开/不同级别保留
    "headings": ["headings", "should_not_touch", "code_fence_protected"],
    "images": ["images_refs"],                     # 正例:引用缺失上报;存在时不动
    # P6 新增的两条**无条件**规则(不是用户开关,恒在):
    "blank_lines": ["whitespace_normalize", "page_numbers", "structure_lines"],
    #   连续 3+ 空行 → 1 个;代码块/脚注/数学块内部不碰
    "trailing_spaces": ["whitespace_normalize", "should_not_touch"],
    #   行尾空白去掉 —— 但「恰好两个空格」的 Markdown 硬换行留着(那是内容)
    # bold 不在此表:它不是纯文本清理规则,而是「开关 → 后端配置」落到后端的字体处理
    # (需要 PDF 字体信息),在 pymupdf 后端侧验证。
}


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _cases() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.md")
                  if not p.name.endswith(".expected.md"))


def _load_evidence(name: str) -> tuple[list, str, int]:
    """读该样本的结构证据层(没有就返回空,页账如实记 undecided)。"""
    path = FIXTURES / f"{name}.evidence.json"
    if not path.exists():
        return [], "markdown", 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, "paddleocr", 0
    items = data.get("items") or []
    return items, str(data.get("backend") or "paddleocr"), int(data.get("total_pages") or 0)


def _run(name: str, md: str, options: CleanOptions,
         images_dir: Path | None) -> tuple[str, tuple[str, ...]]:
    """跑 IR 引擎(与正式流程同一入口 ``clean.clean_text``)。"""
    evidence, backend, total_pages = _load_evidence(name)
    outcome = clean_text(
        md, backend=backend, source=f"{name}.md", evidence=evidence,
        total_pages=total_pages, producer=Producer.from_backend(
            backend, source=f"{name}.md", variant="original"),
        options=options, images_dir=images_dir, write_reports_to=None,
    )
    return outcome.md_after, outcome.issues


@pytest.mark.parametrize("name", _cases())
def test_cleaner_golden(name: str, tmp_path: Path) -> None:
    source = _lf((FIXTURES / f"{name}.md").read_text(encoding="utf-8"))
    expected = _lf((FIXTURES / f"{name}.expected.md").read_text(encoding="utf-8"))

    opts_file = FIXTURES / f"{name}.opts.json"
    opts_data = json.loads(opts_file.read_text(encoding="utf-8")) if opts_file.exists() else {}

    options = CleanOptions()
    options.disable(opts_data.get("disable"))

    images_dir = None
    if opts_data.get("images"):
        images_dir = tmp_path / "images"
        images_dir.mkdir()
        for filename in opts_data["images"]:
            (images_dir / filename).write_bytes(b"")

    actual, issues = _run(name, source, options, images_dir)

    assert actual == expected, f"样本 {name} 的实际输出与期望不一致"
    for issue in opts_data.get("expect_issues", []):
        assert any(issue in i for i in issues), (
            f"{name}: 期望报告 {issue!r},实际 {list(issues)}"
        )


def test_every_case_has_expected_file() -> None:
    missing = [n for n in _cases() if not (FIXTURES / f"{n}.expected.md").exists()]
    assert missing == [], f"缺少期望文件: {missing}"


def test_golden_map_covers_every_rule_and_case() -> None:
    """每条规则都要有样本覆盖,且样本名真实存在(防止改文件名后静默漏测)。

    用户开关(``CLEAN_KEYS``)之外,``blank_lines`` / ``trailing_spaces`` 是恒在的
    结构性规则,也必须守住样本 —— 它们一旦漂移,逐字节比对会当场失败。
    """
    covered = set(GOLDEN_MAP)
    assert set(CLEAN_KEYS) - {"bold"} <= covered, (
        f"没有样本的规则: {sorted(set(CLEAN_KEYS) - {'bold'} - covered)}")
    known_rules = set(PIPELINE_ORDER) | NON_PATCH_RULES
    assert covered <= known_rules, f"非法的规则名: {sorted(covered - known_rules)}"
    known = set(_cases())
    for rule, names in GOLDEN_MAP.items():
        assert names, f"规则 {rule} 没有样本"
        unknown = [n for n in names if n not in known]
        assert not unknown, f"规则 {rule} 引用了不存在的样本: {unknown}"


def test_clean_is_idempotent_on_every_sample(tmp_path: Path) -> None:
    """清理器必须幂等:期望输出再喂一遍不得变化(否则重跑/重转会产生漂移)。"""
    for name in _cases():
        expected = _lf((FIXTURES / f"{name}.expected.md").read_text(encoding="utf-8"))
        again, _issues = _run(name, expected, CleanOptions(), None)
        assert again == expected, f"样本 {name} 不幂等"


def test_clean_is_idempotent_on_raw_inputs_too() -> None:
    """幂等同样要在**未清理的输入**上成立(真实 book.md 的形态)。"""
    for name in _cases():
        source = _lf((FIXTURES / f"{name}.md").read_text(encoding="utf-8"))
        once, _issues = _run(name, source, CleanOptions(), None)
        again, _issues = _run(name, once, CleanOptions(), None)
        assert again == once, f"样本 {name} 清理一次后不再幂等"


def test_samples_without_evidence_do_not_silently_join_across_blocks() -> None:
    """没有证据层的样本:跨块拼接只产建议,md 一个字不动(P6 的安全闸门)。

    ``page_markers`` 之所以能拼,是因为它自带 content_list;这里刻意把证据抽掉,
    确认「拼不动」是被**记录**下来的(建议里有 evidence_insufficient),而不是
    悄悄做了别的事。
    """
    source = _lf((FIXTURES / "page_markers.md").read_text(encoding="utf-8"))
    outcome_clean = clean_text(source, source="page_markers.md", backend="markdown",
                               producer=Producer.from_backend(
                                   "markdown", source="page_markers.md",
                                   variant="original"),
                               total_pages=23, write_reports_to=None)
    # 跨页那两半没有被合并(同一规则下的其它修复 —— 如诗行硬换行 —— 照常发生)
    assert "前半句写到这里后半句在下一页接着写" not in outcome_clean.md_after
    assert outcome_clean.md_after.count("前半句写到这里") == 1
    notes = [n for p in outcome_clean.report.patches
             if p.rule_id == "join_lines" for n in p.validation.notes]
    assert any("缺" in n and "证据" in n for n in notes), notes
    assert outcome_clean.suggested, "缺证据的删除必须作为建议落在报告里(可审计)"
