"""任务状态、重试决策与批量汇总(从 batch.py 拆出)。

批处理最容易出错的地方不是"怎么转一本书",而是**一本书失败之后怎么办**:哪些错误
重试只是白跑一轮(还可能在云端重复计费),哪些值得再试一次,失败的结果怎么在汇总里
如实呈现。这些规则集中在这里,转换流程本身留在 batch.py。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pdf2epub.events as events
from pdf2epub.backends.base import is_retryable
from pdf2epub.clean import CleanEngineError
from pdf2epub.epub.pandoc import PandocMissingError


@dataclass
class TaskResult:
    """单个文件的处理结果(汇总与退出码都看它)。"""

    source: Path
    status: str = "pending"   # done / skipped / failed / pending
    backend: str = ""
    pdf_type: str = ""
    error: str = ""
    elapsed: float = 0.0
    epub: Path | None = None
    verify: str = ""          # 结构校验摘要,如 "0 失败 0 警告"


@dataclass
class BatchSummary:
    """一批任务的计数与耗时。"""

    total: int
    done: int
    skipped: int
    failed: int
    seconds: float

    @classmethod
    def of(cls, results: list[TaskResult]) -> BatchSummary:
        return cls(
            total=len(results),
            done=sum(1 for r in results if r.status == "done"),
            skipped=sum(1 for r in results if r.status == "skipped"),
            failed=sum(1 for r in results if r.status == "failed"),
            seconds=sum(r.elapsed for r in results),
        )


def retry_delay(attempt: int) -> int:
    """第 ``attempt`` 次失败后等多久(秒):指数退避 2/4/8…"""
    return 2 ** attempt


def run_with_retries(work, *, retries: int, file_label: str, logger,
                     deterministic: tuple[type[BaseException], ...] = (CleanEngineError, PandocMissingError)
                     ) -> tuple[TaskResult | None, Exception | None]:
    """反复调用 ``work()`` 直到成功、确定性失败或重试次数用尽。

    返回 ``(成功的 TaskResult, None)`` 或 ``(None, 最后一次异常)``。

    确定性失败(校验不通过 / 清理引擎失败 / 缺 pandoc)当场返回:重试只会把整条
    链路(含云端 OCR 额度)再跑一遍。其余异常按 ``retryable`` 判定,重试对用户可见
    (`retry` 事件),否则「卡住不动」与「正在重试」在界面上分不开。
    """
    last_error: Exception | None = None
    for attempt in range(1, max(retries, 0) + 2):
        try:
            result = work()
        except deterministic as e:
            # 确定性失败:重试只会重复整条链路(含云端 OCR 额度)。
            # 清理失败尤其要说清 —— 它意味着产物不可信,不能静默降级成"没清理就出书"
            if isinstance(e, CleanEngineError):
                logger.error("清理失败(IR 引擎),不再重试: %s", e)
            return None, e
        except Exception as e:  # noqa: BLE001 - 批处理需兜住所有失败
            last_error = e
            # 兜底分支也要留栈:否则只有一句 str(e),排查时不知道是哪个环节抛的。
            # 用 debug 级别 —— 可重试的失败会打很多次,info 日志不该被栈刷满。
            logger.debug("第 %d 次尝试异常堆栈", attempt, exc_info=True)
            if not is_retryable(e):
                logger.error("不可重试的失败,不再重试: %s", e)
                return None, e
            logger.warning("第 %d 次尝试失败(%s): %s", attempt, file_label, e)
            if attempt > retries:
                break
            delay = retry_delay(attempt)
            events.emit("retry", attempt=attempt, retries=retries, wait=delay,
                        message=str(e), file=file_label)
            logger.info("  %ds 后重试...", delay)
            time.sleep(delay)
        else:
            if result.status == "pending":
                result.status = "done"
            return result, None
    return None, last_error
