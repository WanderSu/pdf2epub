"""重试决策与汇总:确定性失败不重试、可重试失败指数退避、汇总计数。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pdf2epub.backends.base import BackendError
from pdf2epub.clean import CleanEngineError
from pdf2epub.taskrun import BatchSummary, TaskResult, retry_delay, run_with_retries

LOG = logging.getLogger("test.taskrun")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("pdf2epub.taskrun.time.sleep", lambda *_: None)


def _transient(msg: str = "网络抖动") -> BackendError:
    return BackendError(msg, retryable=True)


def test_success_first_try_marks_done() -> None:
    result, err = run_with_retries(lambda: TaskResult(source=Path("a.pdf")),
                                   retries=2, file_label="a.pdf", logger=LOG)
    assert err is None and result is not None
    assert result.status == "done"


def test_retryable_error_is_retried_then_succeeds() -> None:
    calls: list[int] = []

    def work() -> TaskResult:
        calls.append(1)
        if len(calls) < 3:
            raise _transient()
        return TaskResult(source=Path("a.pdf"))

    result, err = run_with_retries(work, retries=2, file_label="a.pdf", logger=LOG)
    assert err is None and result is not None and result.status == "done"
    assert len(calls) == 3


def test_retry_exhaustion_returns_last_error() -> None:
    def work() -> TaskResult:
        raise _transient("still down")

    result, err = run_with_retries(work, retries=1, file_label="a.pdf", logger=LOG)
    assert result is None
    assert isinstance(err, BackendError) and "still down" in str(err)


def test_non_retryable_error_stops_immediately() -> None:
    calls: list[int] = []

    def work() -> TaskResult:
        calls.append(1)
        raise BackendError("认证失败", retryable=False, category="auth")

    result, err = run_with_retries(work, retries=3, file_label="a.pdf", logger=LOG)
    assert result is None and isinstance(err, BackendError)
    assert len(calls) == 1


def test_deterministic_error_stops_immediately() -> None:
    calls: list[int] = []

    def work() -> TaskResult:
        calls.append(1)
        raise CleanEngineError("清理炸了")

    result, err = run_with_retries(work, retries=3, file_label="a.pdf", logger=LOG)
    assert result is None and isinstance(err, CleanEngineError)
    assert len(calls) == 1


def test_retry_delay_is_exponential() -> None:
    assert [retry_delay(n) for n in (1, 2, 3)] == [2, 4, 8]


def test_summary_counts_statuses() -> None:
    results = [
        TaskResult(source=Path("a"), status="done", elapsed=1.5),
        TaskResult(source=Path("b"), status="skipped", elapsed=0.5),
        TaskResult(source=Path("c"), status="failed", elapsed=2.0),
        TaskResult(source=Path("d"), status="done", elapsed=1.0),
    ]
    s = BatchSummary.of(results)
    assert (s.total, s.done, s.skipped, s.failed) == (4, 2, 1, 1)
    assert s.seconds == pytest.approx(5.0)
