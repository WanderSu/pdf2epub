"""统一的网络下载保护。

云端结果(JSONL / 图片 / ZIP)都是"别人给的字节",直接 ``requests.get(...).content``
有三个问题:连接阶段没有超时(挂死要等到 TCP 自己放弃)、响应体没有大小上限(一个
异常对象就能把内存或磁盘吃满)、失败时留下半份文件(下次被当成"已有结果")。

这里的约定:

  - 所有请求都带 ``(connect, read)`` 两段超时;
  - 响应体边读边计数,超过上限立即中断;
  - 写盘先写同目录的临时文件,成功才原子替换到目标名 —— 失败不会留下目标文件,
    更不会留下"看起来存在、其实是空的"缓存;
  - 任何异常都清理临时文件。
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import requests

#: 建连超时(秒):对端不接受连接时快速失败,而不是挂到系统默认值
CONNECT_TIMEOUT = 10.0
#: 读超时(秒):云端解析结果包可能很大,读阶段给足时间
READ_TIMEOUT = 300.0
#: 默认的 (connect, read) 组合
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

#: JSONL 结果上限(实测整本 OCR 结果几十 MB 量级)
MAX_JSON_BYTES = 32 * 1024 * 1024
#: 单张图片上限
MAX_IMAGE_BYTES = 32 * 1024 * 1024
#: 结果包 ZIP 上限(与 safe_zip 的总解压预算配合)
MAX_ZIP_BYTES = 512 * 1024 * 1024


class DownloadError(RuntimeError):
    """下载失败(空响应体等非 HTTP 层的异常情况)。"""


class DownloadTooLarge(DownloadError):
    """响应体超过调用方给的字节上限。"""


def _too_large(what: str, limit: int) -> DownloadTooLarge:
    return DownloadTooLarge(f"{what} 超过大小上限 {limit} 字节")


def _content_length(resp: requests.Response) -> int | None:
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Content-Length")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def read_bytes(
    url: str,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    headers: dict | None = None,
    timeout=DEFAULT_TIMEOUT,
    session=requests,
) -> bytes:
    """下载响应体到内存,超过 ``max_bytes`` 直接失败(不会把整份读完再判断)。"""
    with session.get(url, headers=headers, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        declared = _content_length(resp)
        if declared is not None and declared > max_bytes:
            raise _too_large(url, max_bytes)
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise _too_large(url, max_bytes)
            chunks.append(chunk)
    return b"".join(chunks)


def download_to(
    url: str,
    dest: str | Path,
    *,
    max_bytes: int = MAX_ZIP_BYTES,
    headers: dict | None = None,
    timeout=DEFAULT_TIMEOUT,
    session=requests,
) -> Path:
    """流式下载到 ``dest``;失败时目标文件不存在(不会留下半份)。

    先写同目录的 ``.<name>.part-<随机>``,完整读完后 ``os.replace`` 到目标名 ——
    原子替换保证读取方永远看不到写了一半的文件。
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.part-{os.getpid()}-{secrets.token_hex(4)}")
    total = 0
    try:
        with session.get(url, headers=headers, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            declared = _content_length(resp)
            if declared is not None and declared > max_bytes:
                raise _too_large(url, max_bytes)
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise _too_large(url, max_bytes)
                    fh.write(chunk)
        if total == 0:
            # 空响应体不是"成功下载" —— 让它走失败分支,别留下空文件
            raise DownloadError(f"{url} 返回空响应体")
        os.replace(tmp, target)
        return target
    finally:
        tmp.unlink(missing_ok=True)
