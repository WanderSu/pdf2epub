"""MinerU HTTP 客户端单测:鉴权、状态码分类、Token 脱敏、上传失败。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pdf2epub.backends.mineru_http import MinerUClient, MinerUError

TOKEN = "sk-test-0123456789"


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, resp: _Resp) -> None:
        self.resp = resp
        self.calls: list[tuple] = []

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return self.resp

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return self.resp

    def put(self, url, **kw):
        self.calls.append(("put", url, kw))
        return self.resp


def test_missing_token_is_auth_error() -> None:
    with pytest.raises(MinerUError) as ei:
        MinerUClient("")
    assert ei.value.category == "auth"
    assert ei.value.retryable is False


@pytest.mark.parametrize("status,category,retryable", [
    (400, "param", False), (401, "auth", False), (403, "auth", False),
    (429, "quota", False), (500, "network", True), (503, "network", True),
])
def test_status_maps_to_category(status, category, retryable) -> None:
    client = MinerUClient(TOKEN, session=_Session(_Resp(status, text="denied")))
    with pytest.raises(MinerUError) as ei:
        client.get_json("/x")
    assert ei.value.category == category
    assert ei.value.retryable is retryable


def test_response_text_is_redacted() -> None:
    resp = _Resp(401, text=f"Unauthorized: Bearer {TOKEN} rejected")
    client = MinerUClient(TOKEN, session=_Session(resp))
    with pytest.raises(MinerUError) as ei:
        client.post_json("/x", {})
    assert TOKEN not in str(ei.value)


def test_api_code_nonzero_is_not_retryable() -> None:
    resp = _Resp(200, payload={"code": 1003, "msg": "quota exceeded"})
    client = MinerUClient(TOKEN, session=_Session(resp))
    with pytest.raises(MinerUError) as ei:
        client.get_json("/x")
    assert ei.value.category == "param"
    assert ei.value.retryable is False


def test_headers_are_sent_on_every_call() -> None:
    session = _Session(_Resp(200, payload={"code": 0, "data": {}}))
    MinerUClient(TOKEN, "https://host/api/v4/", session=session).get_json("/x")
    method, url, kw = session.calls[0]
    assert method == "get" and url == "https://host/api/v4/x"
    assert kw["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kw["timeout"]  # 两段超时由 net.DEFAULT_TIMEOUT 提供


def test_upload_failure_raises_and_redacts(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    resp = _Resp(403, text=f"forbidden {TOKEN}")
    client = MinerUClient(TOKEN, session=_Session(resp))
    with pytest.raises(MinerUError) as ei:
        client.upload_file("https://upload/x", pdf)
    assert ei.value.category == "auth"
    assert TOKEN not in str(ei.value)
