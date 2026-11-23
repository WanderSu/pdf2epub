"""统一下载保护的用例:超时、体积上限、失败不留半份文件。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pdf2epub.net import (
    DownloadError,
    DownloadTooLarge,
    download_to,
    read_bytes,
)


class _Resp:
    def __init__(self, chunks: list[bytes], *, status: int = 200,
                 content_length: int | None = None) -> None:
        self._chunks = chunks
        self.status_code = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1 << 16):
        yield from self._chunks

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self.resp = resp
        self.calls: list[dict] = []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.resp


def test_download_writes_file_and_sets_timeout(tmp_path: Path) -> None:
    sess = _Session(_Resp([b"abc", b"def"]))
    target = tmp_path / "out.bin"
    assert download_to("https://x/y", target, session=sess) == target
    assert target.read_bytes() == b"abcdef"
    assert sess.calls[0]["timeout"] == (10.0, 300.0)


def test_download_rejects_oversize_and_leaves_nothing(tmp_path: Path) -> None:
    sess = _Session(_Resp([b"x" * 600, b"y" * 600]))
    target = tmp_path / "out.bin"
    with pytest.raises(DownloadTooLarge):
        download_to("https://x/y", target, max_bytes=1000, session=sess)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_declared_oversize_before_reading(tmp_path: Path) -> None:
    sess = _Session(_Resp([b"tiny"], content_length=10_000))
    with pytest.raises(DownloadTooLarge):
        download_to("https://x/y", tmp_path / "o.bin", max_bytes=1000, session=sess)


def test_download_failure_cleans_partial(tmp_path: Path) -> None:
    class Boom(_Resp):
        def iter_content(self, chunk_size: int = 1 << 16):
            yield b"half"
            raise RuntimeError("connection reset")

    target = tmp_path / "o.bin"
    with pytest.raises(RuntimeError):
        download_to("https://x/y", target, session=_Session(Boom([])))
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_empty_body_is_failure_not_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "o.bin"
    with pytest.raises(DownloadError):
        download_to("https://x/y", target, session=_Session(_Resp([])))
    assert not target.exists()


def test_download_keeps_previous_file_when_failing(tmp_path: Path) -> None:
    """失败不能破坏上一次的好结果(原子替换的语义)。"""
    target = tmp_path / "o.bin"
    target.write_bytes(b"old-good")
    with pytest.raises(DownloadTooLarge):
        download_to("https://x/y", target, max_bytes=3, session=_Session(_Resp([b"12345"])))
    assert target.read_bytes() == b"old-good"


def test_read_bytes_caps_size() -> None:
    sess = _Session(_Resp([b"ab", b"cd"]))
    assert read_bytes("https://x/y", session=sess) == b"abcd"
    with pytest.raises(DownloadTooLarge):
        read_bytes("https://x/y", max_bytes=2, session=sess)
