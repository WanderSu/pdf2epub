"""``clean.diff`` —— **只给人看**的展示层(阶段 B §E.7 第 5 条)。

一句话:diff 不能用来恢复任何东西。原因很实际:

- diff 只说"这几行变了",说不出"是哪条规则、凭哪条证据、目标块是哪个";
- 行级差异在"删掉一行空行"与"删掉一整段"之间没有区别;
- 一旦有人手改过 ``book.md``,diff 仍然能算出来 —— 只是算的是另一份文档,
  而 ``clean-report.json`` 里 ``before.sha256`` 会当场对不上。

所以恢复/回滚只读 ``clean-report.json``;``clean.diff`` 的唯一用途是 review。
"""
from __future__ import annotations

import difflib

FROM_FILE = "book.md (before clean)"
TO_FILE = "book.md (after clean)"


def render_unified_diff(before: str, after: str, *, fromfile: str = FROM_FILE,
                        tofile: str = TO_FILE, context: int = 3) -> str:
    """两段 Markdown 的 unified diff;完全相同返回空串。"""
    if before == after:
        return ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=False),
        after.splitlines(keepends=False),
        fromfile=fromfile,
        tofile=tofile,
        n=context,
        lineterm="",
    )
    body = "\n".join(lines)
    return body + "\n" if body else ""
