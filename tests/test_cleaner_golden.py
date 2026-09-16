"""清理器 golden 样本回归(v0.3.2 P0-1)。

一个样本 = 一对文件:

    tests/fixtures/cleaner/<name>.md           输入
    tests/fixtures/cleaner/<name>.expected.md  期望输出(逐字节比对,只把 CRLF 归一为 LF)
    tests/fixtures/cleaner/<name>.opts.json    可选:{"disable": [...], "images": [...],
                                                     "expect_issues": ["图片引用缺失"]}

约定:

- 每条规则都要有「该改的」与「不该改的」两类样本(见 `GOLDEN_MAP`),误伤比漏改严重。
- 新增样本时,**期望输出按「你希望的结果」写**,再跑测试;实际不一致时先判断是代码错
  还是期望写错,不要为了让测试变绿去改期望。改阈值/改行为必须同时改样本并在提交信息里说明原因。
- 期望文件是逐字节比对,所以别手工「顺手」调整空格或结尾换行。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from markdown.cleaner import CLEAN_KEYS, CleanOptions, CleanReport, clean_markdown

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cleaner"

#: 规则 → 覆盖它的样本(至少一个正向样本;含反例的样本会同时列在这里)
GOLDEN_MAP: dict[str, list[str]] = {
    "page_numbers": ["page_numbers"],              # 正例:独立页码行被删;反例:年份 1984、句内数字保留
    "running_heads": ["running_heads"],            # 正例:重复 3 次被删;反例:只出现 2 次的短行保留
    "join_lines": ["join_lines", "poetry_preserved"],   # 正例:断行拼接;反例:代码块/表格/诗句不拼
    "ocr_spaces": ["ocr_spaces"],                  # 正例:Py Mu PDF → PyMuPDF;反例:正常英文短语
    "cjk_spaces": ["cjk_spaces", "should_not_touch"],   # 正例:汉字间空格;反例:中英之间空格保留
    "dup_headings": ["dup_headings"],              # 正例:相邻同名同级去重;反例:被正文隔开/不同级别保留
    "headings": ["headings", "should_not_touch"],  # 正例:空标题删除 + 层级收敛;反例:正常层级不动
    "images": ["images_refs"],                     # 正例:引用缺失上报;存在时不动
    # bold 不在此表:它不是纯文本清理规则,而是「开关 → 后端配置」落到 markdown/bold.py
    # (需要 PDF 字体信息),在 pymupdf 后端侧验证。
}


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _cases() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.md")
                  if not p.name.endswith(".expected.md"))


def _expected_text(name: str) -> str:
    """读期望输出。

    清理器的输出**不带结尾换行**;期望文件按常规文件习惯以换行结尾,所以这里
    恰好去掉一个结尾换行(其余空白一律逐字节比对)。
    """
    raw = _lf((FIXTURES / f"{name}.expected.md").read_text(encoding="utf-8"))
    return raw[:-1] if raw.endswith("\n") else raw


@pytest.mark.parametrize("name", _cases())
def test_cleaner_golden(name: str, tmp_path: Path) -> None:
    source = _lf((FIXTURES / f"{name}.md").read_text(encoding="utf-8"))
    expected = _expected_text(name)

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

    report = CleanReport()
    actual = clean_markdown(source, images_dir=images_dir, report=report, options=options)

    assert actual == expected, f"样本 {name} 的实际输出与期望不一致"
    for issue in opts_data.get("expect_issues", []):
        assert any(issue in i for i in report.issues), (
            f"{name}: 期望报告 {issue!r},实际 {report.issues}"
        )


def test_every_case_has_expected_file() -> None:
    missing = [n for n in _cases() if not (FIXTURES / f"{n}.expected.md").exists()]
    assert missing == [], f"缺少期望文件: {missing}"


def test_golden_map_covers_every_rule_and_case() -> None:
    """每条规则都要有样本覆盖,且样本名真实存在(防止改文件名后静默漏测)。"""
    assert set(GOLDEN_MAP) == set(CLEAN_KEYS) - {"bold"}
    known = set(_cases())
    for rule, names in GOLDEN_MAP.items():
        assert names, f"规则 {rule} 没有样本"
        unknown = [n for n in names if n not in known]
        assert not unknown, f"规则 {rule} 引用了不存在的样本: {unknown}"


def test_clean_is_idempotent_on_every_sample(tmp_path: Path) -> None:
    """清理器必须幂等:期望输出再喂一遍不得变化(否则重跑/重转会产生漂移)。"""
    for name in _cases():
        expected = _expected_text(name)
        assert clean_markdown(expected) == expected, f"样本 {name} 不幂等"
