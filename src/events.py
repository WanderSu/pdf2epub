"""结构化事件流(`--json-events`,v0.4.0 P0-1)。

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
| `error` | `code` / `message` |
| `complete` | `file` / `epub` / `backend` / `pdf_type` / `seconds` |

进度权重之和必须为 1.0(前端按「已完成阶段权重 + 当前阶段权重 × 完成比」算百分比)。
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
    """阶段内进度(如 OCR 的第 N/M 段);`total<=0` 时不发,避免前端除零。"""
    if total > 0:
        emit("progress", stage=stage_name, current=current, total=total, **fields)
