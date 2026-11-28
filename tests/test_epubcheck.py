"""EPUBCheck 集成:解析、模式、与转换流程的接线。

规范检查是可选依赖(需要 Java),所以这里断言的都不是「能不能跑 Java」,而是:
- 报告解析不崩(格式不认识就返回空,不让一次工具升级打断整条转换);
- 模式优先级(环境变量 > 配置 > auto)与非法值退化;
- 没装时**跳过**而不是失败;
- 装了以后 auto 只记警告、on 才判失败 —— 不能因为规范提示就让整本书失败。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import build_epub

import pdf2epub.batch as batch
from pdf2epub.epub import epubcheck as ec


def test_parse_report_keeps_code_and_location() -> None:
    text = json.dumps({"messages": [
        {"ID": "RSC-005", "severity": "ERROR", "message": "bad nav",
         "locations": [{"path": "EPUB/nav.xhtml", "line": 13}]},
        {"ID": "OPF-003", "severity": "WARNING", "message": "item not referenced",
         "locations": []},
        {"severity": "INFO", "message": "无 ID 的条目也不能崩"},
    ]})
    messages = ec.parse_report(text)
    assert [(m.severity, m.code) for m in messages] == [
        ("ERROR", "RSC-005"), ("WARNING", "OPF-003"), ("INFO", "")]
    first = messages[0]
    assert first.path == "EPUB/nav.xhtml" and first.line == 13
    assert messages[1].line is None


def test_parse_report_tolerates_garbage() -> None:
    assert ec.parse_report("<html>不是 JSON</html>") == ()
    assert ec.parse_report(json.dumps([1, 2, 3])) == ()
    assert ec.parse_report(json.dumps({"messages": "oops"})) == ()


def test_resolve_mode_prefers_env_and_degrades_to_auto(monkeypatch) -> None:
    monkeypatch.delenv(ec.MODE_ENV, raising=False)
    assert ec.resolve_mode("on") == "on"
    assert ec.resolve_mode(None) == "auto"
    assert ec.resolve_mode("bogus") == "auto"        # 写错开关不能变成「不检查」
    monkeypatch.setenv(ec.MODE_ENV, "off")
    assert ec.resolve_mode("on") == "off"            # 环境变量覆盖配置


def test_run_epubcheck_without_jar_is_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ec, "find_epubcheck", lambda: None)
    outcome = ec.run_epubcheck(tmp_path / "x.epub")
    assert outcome.ok and outcome.skipped == "epubcheck-missing"


def test_verify_output_records_epubcheck_issues(tmp_path: Path, monkeypatch) -> None:
    """auto = 只记警告(不判失败);on = 规范错误判失败。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "book.epub", title="样本书")
    fake = ec.EpubCheckOutcome(
        ok=False,
        messages=(ec.EpubCheckMessage("ERROR", "RSC-005", "element nav incomplete",
                                      "EPUB/nav.xhtml", 13),),
    )
    monkeypatch.setattr(batch, "run_epubcheck", lambda *_a, **_k: fake)
    monkeypatch.delenv(ec.MODE_ENV, raising=False)   # 测试默认被 conftest 关掉了

    auto = batch.verify_output(epub, book_md=src / "book.md", epubcheck="auto")
    auto_issue = next(i for i in auto.issues if i.code == "epubcheck_error")
    assert auto_issue.level == "warning"
    assert "RSC-005" in auto_issue.message and "nav.xhtml:13" in auto_issue.message

    # on = 规范错误判失败(抛 VerifyError,调用方据此把这本书判失败)
    with pytest.raises(batch.VerifyError, match="RSC-005"):
        batch.verify_output(epub, book_md=src / "book.md", epubcheck="on")


@pytest.mark.skipif(ec.find_epubcheck() is None or not ec.java_available(),
                    reason="本机没有 EPUBCheck(java + jar)")
def test_real_epubcheck_accepts_a_generated_epub(tmp_path: Path) -> None:
    """真跑一次:产出的 EPUB 必须是 0 错误(目录曾用 <ul> 时这里会红)。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "book.epub", title="样本书")
    outcome = ec.run_epubcheck(epub)
    assert outcome.skipped is None
    assert outcome.ok, [m.render() for m in outcome.messages]
    assert outcome.errors == []
