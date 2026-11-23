"""MinerU 结果包解压必须拒绝不可信内容(路径穿越 / 符号链接 / 超限)。

走的是真实入口 ``_store_item``:下载 → 安全解压 → 落盘,而不是只测过滤器本身。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from pdf2epub.backends import mineru_backend
from pdf2epub.backends.base import OcrPart, PartStore
from pdf2epub.backends.mineru_backend import MinerUAdapter, MinerUError


class _Resp:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 1 << 16):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _zip(entries: dict[str, bytes], attrs: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (attrs or {}).get(name, 0o600 << 16)
            zf.writestr(info, data)
    return buf.getvalue()


def _run(tmp_path: Path, content: bytes, monkeypatch) -> Path:
    monkeypatch.setattr(mineru_backend.requests, "get", lambda *a, **k: _Resp(content))
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    store = PartStore(work, "mineru", "fp")
    adapter = MinerUAdapter.__new__(MinerUAdapter)   # 不触发 token 检查
    part = OcrPart(idx=1, part_id="1-3", page_range=None)
    adapter._store_item(store, "params", "original", part,
                        {"full_zip_url": "https://download.local/x"}, work)
    return work


def test_normal_package_is_saved(tmp_path: Path, monkeypatch) -> None:
    content = _zip({
        "1-3/full.md": "# 正文".encode(),
        "1-3/images/a.png": b"png",
        "1-3/layout.json": b"{}",
    })
    work = _run(tmp_path, content, monkeypatch)
    part_dir = work / "_parts" / "1-3"
    assert (part_dir / "full.md").exists()
    assert (part_dir / "images" / "a.png").exists()
    # 不在白名单里的条目不解压
    assert not (part_dir / "layout.json").exists()


def test_path_traversal_is_rejected(tmp_path: Path, monkeypatch) -> None:
    content = _zip({"../escaped.md": b"x", "1-3/full.md": b"md"})
    with pytest.raises(MinerUError) as ei:
        _run(tmp_path, content, monkeypatch)
    assert not (tmp_path / "escaped.md").exists()
    assert ei.value.retryable is False


def test_symlink_member_is_rejected(tmp_path: Path, monkeypatch) -> None:
    content = _zip(
        {"1-3/full.md": b"md", "1-3/evil": b"/etc/passwd"},
        attrs={"1-3/evil": 0o120777 << 16},
    )
    with pytest.raises(MinerUError):
        _run(tmp_path, content, monkeypatch)


def test_missing_full_md_still_reports(tmp_path: Path, monkeypatch) -> None:
    content = _zip({"1-3/images/a.png": b"png"})
    with pytest.raises(MinerUError) as ei:
        _run(tmp_path, content, monkeypatch)
    assert "full.md" in str(ei.value)
