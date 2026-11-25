"""CI 安装固定版本 pandoc（纯标准库，Linux / Windows 通用）。

原 workflow 里 Linux 用 curl 下 deb、Windows 用 Invoke-WebRequest 下 zip，两段逻辑
重复且都没有完整性校验。这里统一成一处：

* 版本、文件名、SHA256 集中成常量；SHA256 取自 GitHub Release API 的 asset
  ``digest`` 字段（与下载通道独立），不是本地算完再自证。
* 下载后先校验再安装，不匹配直接失败；同一次 run 内重复调用命中本地缓存。
* Linux: ``sudo dpkg -i``；Windows: 解压并把 pandoc.exe 所在目录写进 GITHUB_PATH。
* 最后断言 ``pandoc --version`` 首行含目标版本，堵住「装了但版本不对」——
  tests/conftest.py 在缺 pandoc 时会 skip 而不是 fail，静默装错会造成假绿。

字体（fonts-noto-cjk）仍由 workflow 单独 apt 安装，不属于本脚本职责。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

VERSION = "3.14"
RELEASE = f"https://github.com/jgm/pandoc/releases/download/{VERSION}"

# (文件名, sha256)。来源：gh api repos/jgm/pandoc/releases/tags/3.14 --jq '.assets[].digest'
ASSETS = {
    "linux": (
        "pandoc-3.14-1-amd64.deb",
        "dedb81f20345b76d7168ba551d90abceb707977fece789a79874094fe3833915",
    ),
    "windows": (
        "pandoc-3.14-windows-x86_64.zip",
        "0144b96e8c7b023fd382ec5ef5bdc475e873f1019d6fa72502492df2c9a94a3b",
    ),
}


def cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home()))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
    return base / "pdf2epub-pandoc" / VERSION


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path, expected: str) -> Path:
    if dest.exists() and sha256(dest) == expected:
        print(f"[pandoc] 命中已校验缓存: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "pdf2epub-ci/1.0"})
            print(f"[pandoc] 下载 {url}（第 {attempt + 1} 次）")
            with urllib.request.urlopen(req, timeout=180) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
            break
        except Exception as exc:                      # 网络抖动/重定向失败
            last = exc
            dest.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(2)
    else:
        raise RuntimeError(f"下载 pandoc 失败: {last}")

    got = sha256(dest)
    if got != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"pandoc SHA256 不匹配：期望 {expected}，实际 {got}")
    print(f"[pandoc] SHA256 校验通过: {got}")
    return dest


def unpack_windows(zip_path: Path, cache: Path) -> str:
    root = cache / "unpacked"
    exe = next(root.rglob("pandoc.exe"), None) if root.exists() else None
    if exe is None:
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(root)
        exe = next(root.rglob("pandoc.exe"), None)
    if exe is None:
        raise RuntimeError(f"{zip_path} 里找不到 pandoc.exe")

    # 后续步骤是独立进程，必须写 GITHUB_PATH 才能找到 pandoc
    gh_path = os.environ.get("GITHUB_PATH")
    if gh_path:
        with open(gh_path, "a", encoding="utf-8") as fh:
            fh.write(str(exe.parent) + "\n")
    return str(exe)


def assert_version(exe: str) -> None:
    proc = subprocess.run(
        [exe, "--version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or VERSION not in proc.stdout.splitlines()[0]:
        raise RuntimeError(
            f"pandoc 版本不是 {VERSION}: {proc.stdout[:200]} {proc.stderr[:200]}")
    print(f"[pandoc] {proc.stdout.splitlines()[0]}")


def main() -> int:
    platform = "windows" if os.name == "nt" else "linux"
    name, digest = ASSETS[platform]
    cache = cache_dir()
    asset = fetch(f"{RELEASE}/{name}", cache / name, digest)

    if platform == "linux":
        subprocess.run(["sudo", "dpkg", "-i", str(asset)], check=True)
        exe = "pandoc"
    else:
        exe = unpack_windows(asset, cache)

    assert_version(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
