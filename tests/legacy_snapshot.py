"""冷藏的 legacy 清理器输出快照(P6)。

背景:清理规则的行为是在 P1–P5 里对着三本真实书和 18 份 golden 样本一点点校准出来的
(阈值、 bypass 顺序、什么该改什么不该改)。P6 把旧清理器删了 —— **不是**把这些校准
成果一起扔掉:校准确认过的**输出**留在这里(``fixtures/legacy_snapshot.json``),
代码只剩 IR 引擎一套。

为什么是快照而不是「照抄旧behavior的实现」:

- 快照是**数据**:它不会自己漂移,也不会新长出一条代码路径;新引擎的行为是否和旧
  引擎一致,由测试逐字节盯着;
- 一旦有人改了规则/K务阈值而对不上快照,diff 会直接把差异打出来,由人判断是
  「有意的收紧」还是回归 —— 判断权在人手里,不在这一份 JSON 里。

用法::

    from legacy_snapshot import frozen

    assert out.rstrip("\\n") == frozen("join_only", md).rstrip("\\n")

``kind`` 是「当时开了哪些开关」的代号(见后面 ``KINDS`` 说明)。没有对应记录时
``frozen`` 直接抛错并说明该怎么办 —— 新增用例请自己写期望值,不要试图再加回一个
旧引擎当预言机。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).resolve().parent / "fixtures" / "legacy_snapshot.json"

#: 快照的槽位(=「录制时用的开关组合」),新增槽位要在靠近使用的测试里说明形态
KINDS: dict[str, str] = {
    "join_only": "只开 join_lines(含诗行硬换行),其余全部关闭",
    "ocr_spaces_only": "只开 ocr_spaces,其余全部关闭",
    "cjk_spaces_only": "只开 cjk_spaces,其余全部关闭",
    "join_all": "开 join_lines + ocr_spaces + cjk_spaces(页眉/页码/标题关闭)",
    "clean_all": "全部开关取默认值",
    "clean_none": "九个开关全关(只剩空行压缩与行尾空白这两个无条件步骤)",
}

_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        if not SNAPSHOT_PATH.exists():
            raise AssertionError(
                f"缺少 legacy 快照文件: {SNAPSHOT_PATH}"
                "(它随 P6 迁移一起提交,不该被删)")
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        _cache = dict(data.get("records") or {})
    return _cache


def make_key(kind: str, md: str) -> str:
    """槽位 + 输入文本 → 键(只看内容,不看调用位置)。"""
    digest = hashlib.sha256(md.encode("utf-8")).hexdigest()[:16]
    return f"{kind}/{digest}"


def frozen(kind: str, md: str) -> str:
    """取这份输入在旧引擎下的输出(P6 删引擎前逐条录制)。

    没有记录就明确报错 —— 旧引擎已经不在了,想补一个"预言机"只能在端点种种
    写死期望值;静默返回空串会让一整批断言变成空洞的通过。
    """
    key = make_key(kind, md)
    records = _load()
    if key not in records:
        raise AssertionError(
            f"legacy 快照里没有这条输入({key});这条用例是新写的,请直接写出"
            "期望输出(旧清理器已在 P6 删除,不会再加回来当预言机)。"
            f"输入前 60 字:{md[:60]!r}")
    return records[key]


# ---------------------------------------------------------------- 录制(一次性)

#: 录制缓冲:``record`` 写入,``save_records`` 落盘
RECORDS: dict[str, str] = {}


def record(kind: str, md: str, output: str) -> str:
    """**只对 P6 迁移有用**:把一次旧引擎的输出写进缓冲。

    迁移脚本录完快照后就不再调用它;留在模块里是为了让这段历史可查
    (快照文件本身就是用它生成的)。
    """
    RECORDS.setdefault(make_key(kind, md), output)
    return output


def save_records(path: str | Path | None = None) -> int:
    """把缓冲写成快照文件(供 ``frozen`` 读取);返回记录条数。"""
    target = Path(path) if path else SNAPSHOT_PATH
    payload = {"how": "P6 删除 legacy 清理器前录制的输出;见 tests/legacy_snapshot.py",
               "records": RECORDS}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                                 sort_keys=True), encoding="utf-8")
    return len(RECORDS)
