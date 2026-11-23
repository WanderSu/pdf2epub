"""MinerU Cloud 的 HTTP 客户端(协议层)。

只负责「怎么跟 /api/v4 说话」:鉴权头、JSON 解析、状态码分类、文件上传。分片、
续跑、结果合并都在适配器里 —— 那几件事依赖缓存与磁盘,和协议无关。

错误消息统一经 :func:`pdf2epub.paths.redact_secret` 抹掉自身 Token:它们会进日志、
事件流和界面,而云端有时会把请求内容回显在错误文本里。
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from pdf2epub.net import DEFAULT_TIMEOUT
from pdf2epub.paths import redact_secret

from .base import BackendError

DEFAULT_BASE_URL = "https://mineru.net/api/v4"

#: 客户端类 HTTP 状态码:认证/参数/配额问题,重试改变不了结果,只会重复消耗额度
NON_RETRYABLE_HTTP = {400, 401, 403, 404, 405, 409, 413, 422, 429}


class MinerUError(BackendError):
    """MinerU API 错误。

    err_msg 是**云端返回的原始错误信息**:降级判定只看它,不看我们自己拼的中文前缀 ——
    否则 `MinerU 解析失败: xxx` 这类消息会让任何失败都被误判成「解析失败」而触发
    渲染纯图重试(白跑一轮、重复消耗额度)。
    """

    def __init__(self, message: str, *, err_msg: str | None = None,
                 retryable: bool | None = None, category: str | None = None) -> None:
        super().__init__(message, retryable=retryable, category=category)
        self.err_msg = message if err_msg is None else err_msg


def category_for_status(status: int) -> str:
    """HTTP 状态码 → 粗分类(只用于展示;要不要重试仍看 ``retryable``)。"""
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "quota"
    if status in (400, 404, 405, 409, 413, 422):
        return "param"
    if status >= 500:
        return "network"
    return "unknown"


def _missing_token_error() -> MinerUError:
    return MinerUError(
        "缺少 MinerU API Token:请设置环境变量 MINERU_API_TOKEN 或项目根目录 apikey.json",
        retryable=False,     # 认证缺失重试多少次都一样
        category="auth",
    )


class MinerUClient:
    """一组 /api/v4 调用。``session`` 传模块对象或 requests.Session 均可。

    测试把 ``mineru_backend.requests`` 换成假云端时,适配器会用当前模块属性构造
    客户端,所以这里不缓存 session(每次调用都读一次)。
    """

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, *, session=None) -> None:
        if not token:
            raise _missing_token_error()
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._session = session or requests

    # ---------- 请求 ----------
    @property
    def headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def redact(self, text: str) -> str:
        return redact_secret(text, self.token)

    def parse(self, resp: requests.Response) -> dict:
        if resp.status_code != 200:
            raise MinerUError(
                self.redact(f"HTTP {resp.status_code}: {resp.text[:300]}"),
                retryable=resp.status_code not in NON_RETRYABLE_HTTP,
                category=category_for_status(resp.status_code),
            )
        data = resp.json()
        if data.get("code") != 0:
            # API 层判定的错误(参数/配额/权限):重试无意义
            raise MinerUError(
                self.redact(f"API 错误 code={data.get('code')} msg={data.get('msg')}"),
                retryable=False,
                category="param",
            )
        return data

    def post_json(self, path: str, payload: dict) -> dict:
        resp = self._session.post(f"{self.base_url}{path}", headers=self.headers,
                                  json=payload, timeout=DEFAULT_TIMEOUT)
        return self.parse(resp)

    def get_json(self, path: str) -> dict:
        resp = self._session.get(f"{self.base_url}{path}", headers=self.headers,
                                 timeout=DEFAULT_TIMEOUT)
        return self.parse(resp)

    def upload_file(self, url: str, path: str | Path) -> None:
        """PUT 上传(PyMuPDF/MinerU 要求不设置 Content-Type)。"""
        with open(path, "rb") as fh:
            resp = self._session.put(url, data=fh, timeout=DEFAULT_TIMEOUT)
        if resp.status_code not in (200, 201):
            raise MinerUError(
                self.redact(f"文件上传失败 HTTP {resp.status_code}: {resp.text[:200]}"),
                retryable=resp.status_code not in NON_RETRYABLE_HTTP,
                category=category_for_status(resp.status_code),
            )


def open_client(token: str | None, base_url: str | None, *, session=None) -> MinerUClient:
    """按「显式参数 → 环境变量 → apikey.json」取 Token 后构造客户端。"""
    from pdf2epub.paths import load_api_key

    resolved = token or os.environ.get("MINERU_API_TOKEN", "") or load_api_key("MinerU") or ""
    return MinerUClient(resolved, base_url or DEFAULT_BASE_URL, session=session)
