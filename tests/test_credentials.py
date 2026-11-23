"""凭证处理:不外泄、收紧后复核。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pdf2epub import paths
from pdf2epub.backends.mineru_backend import MinerUAdapter, MinerUError

TOKEN = "sk-live-0123456789abcdef"


class _Resp:
    status_code = 401

    def __init__(self, text: str) -> None:
        self.text = text

    def json(self) -> dict:  # pragma: no cover - 401 分支不会走这里
        return {}


def _adapter() -> MinerUAdapter:
    adapter = MinerUAdapter.__new__(MinerUAdapter)      # 跳过构造时的网络/凭证检查
    adapter.token = TOKEN
    return adapter


def test_error_message_redacts_token() -> None:
    resp = _Resp(f"Unauthorized: Bearer {TOKEN} is invalid")
    with pytest.raises(MinerUError) as ei:
        _adapter()._parse(resp)
    assert TOKEN not in str(ei.value)
    assert "***" in str(ei.value)


def test_api_error_message_redacts_token() -> None:
    class _Ok:
        status_code = 200
        text = ""

        def json(self) -> dict:
            return {"code": 1003, "msg": f"token {TOKEN} expired"}

    with pytest.raises(MinerUError) as ei:
        _adapter()._parse(_Ok())
    assert TOKEN not in str(ei.value)


def test_redact_secret_ignores_short_values() -> None:
    # 太短的值替换会误伤正文,只处理 ≥8 位的真实 Token
    assert paths.redact_secret("run with t now", "t") == "run with t now"
    assert paths.redact_secret("x abcdefghij y", "abcdefghij") == "x *** y"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位语义")
def test_verify_permissions_posix(tmp_path: Path) -> None:
    f = tmp_path / "apikey.json"
    f.write_text("{}", encoding="utf-8")
    os.chmod(f, 0o600)
    assert paths.verify_api_key_permissions(f) is True
    os.chmod(f, 0o644)
    assert paths.verify_api_key_permissions(f) is False


def test_verify_permissions_missing_file(tmp_path: Path) -> None:
    assert paths.verify_api_key_permissions(tmp_path / "nope.json") is False
