"""异常分类:只用于展示,不改变 retryable 语义。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pdf2epub.backends.base import ERROR_CATEGORIES, BackendError, is_retryable
from pdf2epub.backends.mineru_backend import MinerUAdapter, MinerUError


class _Resp:
    def __init__(self, status: int, text: str = "boom") -> None:
        self.status_code = status
        self.text = text

    def json(self) -> dict:  # pragma: no cover - 非 200 不走
        return {}


def _adapter() -> MinerUAdapter:
    adapter = MinerUAdapter.__new__(MinerUAdapter)
    adapter.token = "unit-test-token"
    return adapter


@pytest.mark.parametrize("status,category,retryable", [
    (400, "param", False),
    (401, "auth", False),
    (403, "auth", False),
    (429, "quota", False),
    (500, "network", True),
    (502, "network", True),
])
def test_http_status_maps_to_category(status: int, category: str, retryable: bool) -> None:
    with pytest.raises(MinerUError) as ei:
        _adapter()._parse(_Resp(status))
    assert ei.value.category == category
    # 分类不改变重试语义
    assert is_retryable(ei.value) is retryable


def test_category_vocabulary_is_closed() -> None:
    err = MinerUError("x")
    assert err.category == "unknown"
    assert err.category in ERROR_CATEGORIES


def test_missing_file_is_input_error(tmp_path: Path) -> None:
    adapter = _adapter()
    with pytest.raises(MinerUError) as ei:
        adapter.convert(tmp_path / "nope.pdf", tmp_path / "work")
    assert ei.value.category == "input"
    assert ei.value.retryable is False


def test_backend_error_defaults_keep_retryable() -> None:
    assert BackendError("x").retryable is True
    assert BackendError("x", retryable=False).retryable is False
