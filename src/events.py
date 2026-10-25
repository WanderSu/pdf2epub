"""结构化事件流(`--json-events`)。

**为什么**:桌面端此前用 5 个正则从 CLI 的**中文日志**里猜状态 —— 文案一改就静默
失效,进度百分比只能按「日志行到达 +5」估算。现在引擎把阶段事件以 **JSON Lines**
写给机器消费:

- 开启 `--json-events` 后:stdout 只走 JSON 事件(人类日志改道 stderr),
  桌面端读 stdout 即可拿到真实状态:类型、页数、后端、分片/区段、阶段与进度。
- 未开启时:一切照旧(日志仍在 stdout),`emit()` 是空操作 —— CI/脚本场景日志更可读。

事件表(每行一个 JSON 对象,必含 `event`):

| event | 关键字段 |
|---|---|
| `hello` | `stages`: [{name, weight}] —— 前端据此算真实进度,不用两边各写一份权重 |
| `detect` | `type` / `pages` / `text_pages` / `text_ratio` / `suspicious_pages` |
| `plan` | `backend` / `pages` / `ocr_pages` / `ocr_runs` / `shards` |
| `stage` | `name`(detect/extract/clean/build/verify)+ `state`(start/done) |
| `progress` | `stage` / `current` / `total` / `detail` |
| `shards` | `total` / `ranges`(MinerU 分片的真实页码区间) |
| `verify` | `errors` / `warnings` / `message` |
| `warning` | `code` / `message` |
| `retry` | `attempt` / `retries` / `wait` / `message`(第 N 次尝试失败、即将重跑) |
| `error` | `code` / `message`(见 `error_code()`:GUI 按 code 分类显示) |
| `skip` | `file` / `epub`(**已有产物,本次未转换** —— 不是 complete) |
| `complete` | `file` / `epub` / `backend` / `pdf_type` / `seconds` |

进度权重之和必须为 1.0(前端按「已完成阶段权重 + 当前阶段权重 × 完成比」算百分比)。

**`skip` 与 `complete` 是两种终态**,别让消费者把它们折算成同一个「成功」:前者是
「本次什么都没做」(产物早已存在),后者才是「本次跑完了整条链路」。
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO

#: 阶段与权重:前端按它算真实进度(不要在前端再写一份,会分叉)
STAGES: tuple[tuple[str, float], ...] = (
    ("detect", 0.05),
    ("extract", 0.60),
    ("clean", 0.15),
    ("build", 0.15),
    ("verify", 0.05),
)


class EventSink:
    """把事件写成 JSON Lines(每行一个对象 + flush,便于逐行读)。"""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.count = 0

    def emit(self, event: str, **fields: Any) -> None:
        payload = {"event": event, "ts": round(time.time(), 3), **fields}
        line = json.dumps(payload, ensure_ascii=False)
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            return          # 管道被关闭等:事件丢失不该影响转换
        self.count += 1


_sink: EventSink | None = None


def configure(enabled: bool = True, stream: TextIO | None = None) -> EventSink | None:
    """开启/关闭事件流。返回 sink(关闭时为 None),便于测试断言。

    重复调用会替换 sink;`configure(False)` 之后 `emit()` 变成空操作。
    """
    global _sink
    _sink = EventSink(stream) if enabled else None
    if _sink is not None:
        _sink.emit("hello", stages=[{"name": n, "weight": w} for n, w in STAGES])
    return _sink


def enabled() -> bool:
    return _sink is not None


def emit(event: str, **fields: Any) -> None:
    """发一条事件;未开启时什么都不做(调用点不需要判空)。"""
    if _sink is not None:
        _sink.emit(event, **fields)


def stage(name: str, state: str, **fields: Any) -> None:
    """阶段开始/结束(`state` ∈ start/done)。"""
    emit("stage", name=name, state=state, **fields)


def progress(stage_name: str, current: int, total: int, **fields: Any) -> None:
    """阶段内进度(如 OCR 的第 N/M 段);`total<=0` 时不发,避免前端除零。

    若当前处于 ``progress_window`` 内,``(current, total)`` 会被重映射到窗口
    对应的份额:窗口 i/N 内的 (c,t) → 全局 [(i-1)/N + (c/t)/N]。否则段内后端
    自报的 (1,1) 会被前端的「取峰值」当成整个 extract 阶段 100%,进度条提前
    跳满再长时间不动(hybrid 每段一次云端任务时必现)。
    """
    if total <= 0:
        return
    window = _progress_window
    if window is not None and window[0] == stage_name:
        _, index, outer_total = window
        scale = 1000
        current = (index - 1) * scale + min(scale, round(scale * current / total))
        total = outer_total * scale
    emit("progress", stage=stage_name, current=current, total=total, **fields)


#: 当前生效的进度窗口:``(stage_name, index, outer_total)``,index 从 1 起。
#: 转换是单线程串行,模块级状态足够;窗口只影响同 stage 的 progress 事件。
_progress_window: tuple[str, int, int] | None = None


class progress_window:
    """把窗口内同 stage 的 progress 事件重映射到第 ``index``/``total`` 段的份额。

    用法(上层按段循环时)::

        for i, run in enumerate(runs, start=1):
            events.progress("extract", i, len(runs), detail="hybrid_ocr")
            with events.progress_window("extract", i, len(runs)):
                ocr.convert(...)        # 后端自报的 (1,1) 被折算进第 i 段
    """

    def __init__(self, stage_name: str, index: int, total: int) -> None:
        self.stage_name = stage_name
        self.index = index
        self.total = total
        self._prev: tuple[str, int, int] | None = None

    def __enter__(self) -> None:
        global _progress_window
        self._prev = _progress_window
        _progress_window = (self.stage_name, self.index, self.total)

    def __exit__(self, *exc: Any) -> None:
        global _progress_window
        _progress_window = self._prev


#: 异常类名 → 错误码。GUI 按错误码分类显示(输入/配置/环境/OCR/输出/校验),
#: 从而不必去解析中文文案。**按类名而非 isinstance 匹配**:batch 会 import events,
#: 这里反向 import 会形成循环;对应的真实异常类在测试里逐一钉住(改名会当场失败)。
ERROR_CODE_BY_TYPE: dict[str, str] = {
    "PandocMissingError": "missing_dependency",   # 环境缺失(pandoc 不在 PATH)
    "PandocError": "pandoc_error",              # pandoc 执行失败(非零退出 / 声称成功却没写出产物)
    "VerifyError": "verify_failed",               # 产物校验未通过(致命问题,非 --strict 同样报)
    "VerifyStageError": "verify_failed",          # 校验过程自身出错(不是「不通过」:可重试)
    "CleanEngineError": "clean_failed",           # IR 清理失败(不回退,不静默放过)
    "MinerUError": "ocr_failed",
    "PaddleOCRError": "ocr_failed",
    "BackendError": "backend_failed",
    "FileNotFoundError": "input_error",
    "FileDataError": "input_error",               # PyMuPDF:文档损坏/打不开
    "EmptyFileError": "input_error",
    "IsADirectoryError": "input_error",
    "NotADirectoryError": "input_error",
    "PermissionError": "output_error",            # 写盘/文件被占用(读取侧走 PyMuPDF 自己的异常)
    "OSError": "output_error",
    "ValueError": "config_error",
    "KeyError": "config_error",
}


def error_code(error: BaseException, default: str = "convert_failed") -> str:
    """异常 → 事件里的 `code`(前端据此分类显示错误,不改异常体系本身)。

    沿异常的 MRO 取第一个命中的类名(子类优先:`MinerUError` 归 OCR,退一步才是
    `BackendError`);认不出来时返回调用方给的 `default`(如 `name_conflict`、
    `convert_failed`),也就是「未知/意外」这一类。

    **不按文案猜**:pandoc 的两条执行失败已有专属类 ``PandocError``。旧实现在这里
    按「类名是 RuntimeError 且文案含 pandoc」兜底,改成专属类之后那条判断会**静默
    失效**、错误码退化成 ``convert_failed``(前端从 PANDOC 变成 UNKNOWN)。这条兜底
    已删除:新增异常类必须登记到 ``ERROR_CODE_BY_TYPE``。
    """
    for cls in type(error).__mro__:
        code = ERROR_CODE_BY_TYPE.get(cls.__name__)
        if code:
            return code
    return default
