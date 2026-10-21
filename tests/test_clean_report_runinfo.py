"""清理报告 ``run`` 段必须如实记录「这次开着哪些开关、跑了哪些规则」(P7 验收发现)。

验收三本缓存书时发现:``公社``/``自然辩证法`` 走 Markdown 入口一条规则都没命中,
落盘的 ``clean-report.json`` 里

- ``run.rules_enabled`` 是 ``[]``;
- ``run.options`` 是全默认值。

而它们的 ``run.options`` 明明写着九项全开、``engine`` 是 ``"ir"`` —— 报告自相矛盾。

根因在两处:

1. ``clean.rules.propose_rules`` 只为「确实提出了 Patch」的规则建报告
   (``if not proposal.patches: continue``),一条都没命中时 ``reports`` 是空列表;
2. ``clean.pipeline.merge_reports`` 从 ``reports[0].run`` 取 ``options`` 与
   ``rules_enabled``,``reports`` 为空时退化成 ``normalize_options(None)``
   (= 全默认) 和 ``()``。

后果不只是难看:命令行 ``--clean-disable join_lines`` 且恰好没命中时,报告会说
``join_lines: true`` —— 审计记录反向撒谎,而 ``clean-report`` 正是用来回放与追责的。

修复:``merge_reports`` 改成以调用方的真实入参为准(``options`` / ``rule_ids``),
``reports`` 只用来取 Patch 与时间戳。
"""
from __future__ import annotations

from clean.options import CleanOptions
from clean.pipeline import clean_text

#: 一条规则都不会命中的纯文本(没有页码行、空行、异常空格、重复标题……)
CLEAN_MD = "# 书名\n\n一段很普通的中文正文,没有任何需要清理的东西。\n"


def test_no_patch_run_still_records_enabled_rules() -> None:
    """一条都没命中时,rules_enabled 必须是「开了哪些」,不是空的。"""
    outcome = clean_text(CLEAN_MD, backend="markdown", source="plain.md")
    assert outcome.applied == 0

    rules = outcome.report.run.rules_enabled
    assert rules, "rules_enabled 为空:报告自称什么规则都没跑"
    for expected in ("page_numbers", "join_lines", "cjk_spaces", "headings"):
        assert expected in rules, f"{expected} 实际开着,却没记进报告: {rules}"


def test_no_patch_run_records_real_options_not_defaults() -> None:
    """关掉的开关要如实记成 False —— 不能因为没命中就退回全默认值。"""
    options = CleanOptions(join_lines=False, cjk_spaces=False)
    outcome = clean_text(CLEAN_MD, backend="markdown", source="plain.md",
                         options=options)

    got = outcome.report.run.options
    assert got["join_lines"] is False, f"join_lines 已关闭,报告却写 {got['join_lines']}"
    assert got["cjk_spaces"] is False, f"cjk_spaces 已关闭,报告却写 {got['cjk_spaces']}"
    assert got["page_numbers"] is True      # 没关的仍是 True


def test_enabled_rules_follow_disabled_options() -> None:
    """关掉的规则不出现在 rules_enabled 里(rules_enabled = 真正会跑的那些)。"""
    outcome = clean_text(CLEAN_MD, backend="markdown", source="plain.md",
                         options=CleanOptions(join_lines=False))
    assert "join_lines" not in outcome.report.run.rules_enabled
    assert "page_numbers" in outcome.report.run.rules_enabled


def test_patched_run_is_unaffected() -> None:
    """有命中的情况下,报告与改动后一致(防止修出新偏差)。"""
    md = "# 书名\n\n# 书名\n\n正文。\n"          # 重复标题 → dup_headings 命中
    outcome = clean_text(md, backend="markdown", source="dup.md")
    assert outcome.applied >= 1
    assert "dup_headings" in outcome.report.run.rules_enabled
    assert outcome.report.run.options["dup_headings"] is True
