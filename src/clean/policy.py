"""清理规则的**应用策略**:哪条规则在正式流程里可以直接改正文,凭什么。

为什么需要这个模块:引擎(``clean.engine``)只认 ``confidence`` 与 ``evidence``,
它不知道"这条规则在真实书上有没有被验证过"。而正式转换流程必须回答两个问题:

1. 哪些规则的候选**默认就该落地**(否则清理等于没做);
2. 哪些必须停在 ``suggested``(证据不足,只记建议)。

把答案写成一张表,而不是散落在调用点的布尔开关,理由是可审计:报告里的每条 Patch
都能回查到它为什么被允许执行(``confidence`` + 本表 ``auto_apply`` + 引擎的证据闸门)。

三条线不放松(与 P2–P5 一致):

- ``confidence="heuristic"`` 的 Patch 默认**只产建议**;只有这里显式标了
  ``auto_apply`` 的规则才允许在正式流程里落地,而且**必须说明凭什么**;
- ``delete_block``/``merge_blocks`` 仍然要过 ``evidence.sufficient`` 或"整块自身命中"
  (见 ``clean.engine._structural_self_evidence``),策略表不改变这条;
- 源指纹、before/after 哈希、依赖、手改拒绝四道闸门与策略无关,永远生效。

``PIPELINE_ORDER`` 也放这里:顺序本身是策略的一部分(空行压缩必须先于拼接,
行尾空白必须先于诗行硬换行),散开写必然漂移。
"""
from __future__ import annotations

from dataclasses import dataclass

from clean.options import CleanOptions


@dataclass(frozen=True)
class RulePolicy:
    """一条规则在正式流程里的定位。"""

    rule_id: str
    confidence: str
    auto_apply: bool
    why: str


#: 规则表的**顺序即执行顺序**(与 legacy 管线同源,逐条对应它的步骤号):
#:
#:   2 页号码  3 页眉页脚  4 中文空格  5 空行压缩  10 行尾空白
#:   6 拼接  7 OCR 空格  8 标题层级 / 重复标题
#:
#: 三处刻意的顺序调整(都必须如此,否则互相打架):
#:   - 行尾空白排在拼接**之前**:拼接会顺手 strip 它碰到的行,而诗行硬换行(挂在
#:     join_lines 名下)是"行尾两空格",压空白若排在后面会把硬换行一起吃掉;
#:   - 空行压缩排在拼接之前:拼接看到的空行数恒 ≤1(legacy 同视角);
#:   - **标题层级排在重复标题去重之前**(P6 改):去重要比的是读者看到的**最终**
#:     级别。旧顺序先去重再收敛层级,于是 `## 书名` + `# 书名` 这种"同一标题被 OCR
#:     标了两级"的首个标题在这一次运行时先被提升成 `#`、仍是两个不同级别 → 去重
#:     看不上,只有**第二遍清理**才会把它删掉(实测巴勒斯坦一书:旧顺序第一遍
#:     applied 12 条、第二遍又冒出一条 dup_headings;现在的顺序第一遍 13 条、第二遍 0)。
POLICY: tuple[RulePolicy, ...] = (
    RulePolicy(
        "page_numbers", "derived", True,
        "判据是整行正则(纯数字 1-3 位 / 「第 N 页」/「— N —」),不是猜测;"
        "legacy 无条件执行同一条正则,18 份 golden 样本覆盖正反例",
    ),
    RulePolicy(
        "running_heads", "derived", True,
        "判据是可数的:同一短行全文出现 ≥3 次、无句末标点、非块结构、非目录条目/注释;"
        "阈值(3 次 / 40 字)与 legacy 相同,golden 样本含「只出现 2 次不删」的反例",
    ),
    RulePolicy(
        "cjk_spaces", "heuristic", True,
        "中文排版空格的修正是启发式(可能碰到本来就该有空格的行),但判据与 legacy 同源,"
        "且已在三本真实书上逐字节验收;标题/目录条目/注释/版式字段行一律跳过",
    ),
    RulePolicy(
        "blank_lines", "derived", True,
        "判据是「这里有几个连续换行」——可直接数出来,不是猜测,legacy 也把它当无条件步骤",
    ),
    RulePolicy(
        "trailing_spaces", "derived", True,
        "行尾空白是确定事实(正则 [ \\t]+$),legacy 无条件清理;代码/公式/脚注/注释块"
        "整体跳过(它们的行尾空白是内容)",
    ),
    RulePolicy(
        "join_lines", "heuristic", True,
        "跨页断行拼接是启发式,但判据(排满一行的门槛 20 字 / 诗行 / 目录条目 / 版式字段行)"
        "与 legacy 逐条同源;跨块拼接的**删除**仍要求被吸收块有结构证据,"
        "缺证据只产建议(正文不并、块不删,绝不产生重复内容)",
    ),
    RulePolicy(
        "ocr_spaces", "heuristic", True,
        "OCR 拆词合并是启发式(可能碰到正常英文短语),判据三条同时成立才动,"
        "且只在含中文的行上生效;与 legacy 同源",
    ),
    RulePolicy(
        "headings", "derived", True,
        "空标题(只有 #)与层级跳跃(> 上一级 +1)都是结构事实;只改 # 的个数,不动标题文字。"
        "排在去重之前:去重要比的是收敛后的最终级别(见上面的顺序说明)",
    ),
    RulePolicy(
        "dup_headings", "derived", True,
        "判据是「相邻、同级别、同文字」,可直接比对;被正文隔开的同名标题一律保留",
    ),
)

#: 规则执行顺序(见上面对三处调整的说明)
PIPELINE_ORDER: tuple[str, ...] = tuple(p.rule_id for p in POLICY)

_BY_ID: dict[str, RulePolicy] = {p.rule_id: p for p in POLICY}

#: 不是"文本改写规则"的开关:``bold`` 落到后端字体配置,``images`` 是校验(只报 issue)
NON_PATCH_RULES: frozenset[str] = frozenset({"bold", "images"})


def policy_for(rule_id: str) -> RulePolicy:
    """取规则策略;未登记即报错(不猜默认值 —— 新规则必须先表态)。"""
    try:
        return _BY_ID[rule_id]
    except KeyError:
        raise KeyError(f"未登记的规则: {rule_id!r}(允许: {', '.join(PIPELINE_ORDER)})") from None


def enabled_rules(options: CleanOptions) -> tuple[str, ...]:
    """按 ``CleanOptions`` 过滤后的执行顺序(``blank_lines`` / ``trailing_spaces``
    不是用户开关,恒在)。"""
    always_on = {"blank_lines", "trailing_spaces"}
    return tuple(rule for rule in PIPELINE_ORDER
                 if rule in always_on or getattr(options, rule, True))


def auto_apply_rules(options: CleanOptions | None = None) -> frozenset[str]:
    """正式流程里允许自动落地 ``heuristic`` 候选的规则集合。"""
    enabled = set(enabled_rules(options)) if options is not None else set(PIPELINE_ORDER)
    return frozenset(p.rule_id for p in POLICY
                     if p.auto_apply and p.confidence == "heuristic"
                     and p.rule_id in enabled)
