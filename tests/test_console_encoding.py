"""控制台编码不是 UTF-8 时,输出只降级、不崩。

真实场景:Windows 的 cp1252 / cp936 控制台,冻结成 exe 后不认 PYTHONUTF8;
日志里的 → ✓ 这类字符会让 print 抛 UnicodeEncodeError,把整次转换打死。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="用 Windows 的 cp1252 代码页复现")
def test_help_does_not_crash_on_cp1252_console() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from pdf2epub.cli import main; raise SystemExit(main(['--help']))"],
        env=env, capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-800:]
    assert b"UnicodeEncodeError" not in proc.stderr
