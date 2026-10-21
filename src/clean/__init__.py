"""IR 清理引擎的契约层、执行引擎与正式管线(P6:唯一清理实现)。

    from clean import clean_book_md          # 正式流程(批处理走这条)
    from clean import Patch, apply_patches   # 底层:自己组装 Patch 时用

模块分工:

- ``patch``    一条 Patch 的字段、枚举与序列化(含"未知枚举读成 unknown"规则)
- ``options``  清理开关(CleanOptions / CLEAN_KEYS / resolve_options)
- ``policy``   应用策略:哪条规则允许自动落地、凭什么;以及规则执行顺序
- ``report``   ``clean-report.json`` 的信封、统计与确定性读写
- ``diff``     仅供展示的 unified diff(**不能**用于恢复)
- ``engine``   双校验应用 / 逆序撤销 / 状态与统计的落账
- ``rules``    九条规则的候选提案(只提候选,不改正文)
- ``pipeline`` 正式入口:Markdown → IR → 规则 → Patch → Markdown + 报告

规则层只负责"提出候选",是否能改由 ``engine`` 依据 ``confidence``、``evidence``
与 ``policy`` 判定。三个层次任何一层都不"顺手"改正文。
"""
from __future__ import annotations

from clean.diff import render_unified_diff
from clean.engine import ApplyResult, apply_patches, revert_patches
from clean.options import CLEAN_KEYS, CleanOptions, normalize_clean_keys, resolve_options
from clean.patch import (CONFIDENCES, EVIDENCE_SCOPES, EVIDENCE_SOURCES,
                         OPERATIONS, PREFIX_TARGET, RISKS, RULE_IDS,
                         SCHEMA_VERSION, SKIP_REASONS, STATUSES, AfterState,
                         BlockState, Evidence, Patch, PatchError,
                         ReportVersionError, SourceFingerprint, Validation,
                         sort_patches)
from clean.pipeline import (CleanEngineError, CleanOutcome, clean_book_md,
                            clean_text, merge_reports, normalize_newlines,
                            policy_table, write_artifacts)
from clean.policy import (PIPELINE_ORDER, POLICY, RulePolicy, auto_apply_rules,
                          enabled_rules, policy_for)
from clean.report import (DIFF_FILE, REPORT_FILE, CleanReport, RunInfo, Stats,
                          Verification, default_options, normalize_options,
                          read_report, write_report)
from clean.rules import (BLANK_RUN_RE, RuleProposal, check_image_refs,
                         compress_blank_runs, propose_blank_lines,
                         propose_cjk_spaces, propose_dup_headings,
                         propose_headings, propose_join_lines,
                         propose_ocr_spaces, propose_page_numbers,
                         propose_rules, propose_running_heads,
                         propose_trailing_spaces)

__all__ = [
    "SCHEMA_VERSION", "RULE_IDS", "OPERATIONS", "STATUSES", "SKIP_REASONS",
    "PREFIX_TARGET",
    "CONFIDENCES", "RISKS", "EVIDENCE_SOURCES", "EVIDENCE_SCOPES",
    "Patch", "PatchError", "ReportVersionError",
    "BlockState", "AfterState", "Evidence", "SourceFingerprint", "Validation",
    "CleanReport", "RunInfo", "Stats", "Verification",
    "REPORT_FILE", "DIFF_FILE",
    "read_report", "write_report", "default_options", "normalize_options",
    "render_unified_diff", "sort_patches",
    "apply_patches", "revert_patches", "ApplyResult",
    "CLEAN_KEYS", "CleanOptions", "normalize_clean_keys", "resolve_options",
    "POLICY", "PIPELINE_ORDER", "RulePolicy", "auto_apply_rules",
    "enabled_rules", "policy_for",
    "RuleProposal",
    "propose_page_numbers", "propose_running_heads", "propose_trailing_spaces",
    "propose_join_lines", "propose_ocr_spaces", "propose_cjk_spaces",
    "propose_dup_headings", "propose_headings",
    "propose_blank_lines", "compress_blank_runs", "BLANK_RUN_RE",
    "propose_rules", "check_image_refs",
    "CleanEngineError", "CleanOutcome", "clean_book_md", "clean_text",
    "merge_reports", "normalize_newlines", "policy_table", "write_artifacts",
]
