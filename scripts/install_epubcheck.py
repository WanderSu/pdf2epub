"""下载并解压 EPUBCheck 到 `.tools/epubcheck/`(开发/CI 用,不入库)。

    uv run python scripts/install_epubcheck.py
    uv run python scripts/install_epubcheck.py --force

产物:`.tools/epubcheck/epubcheck-<版本>/epubcheck.jar`。引擎的
`epub/epubcheck.py::find_epubcheck()` 会自动在 `.tools/` 下找到它。

**为什么要钉 sha256**:下载的是**要直接交给 java 执行**的 jar;只靠 HTTPS + 体积
判断挡不住投毒/镜像替换。版本升级时一起换 URL 与 SHA256(来源:GitHub Release
资产页的 digest)。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS = PROJECT_ROOT / ".tools" / "epubcheck"

VERSION = "5.4.0"
URL = f"https://github.com/w3c/epubcheck/releases/download/v{VERSION}/epubcheck-{VERSION}.zip"
SHA256 = "33350c61038e71dfb3d45a76aed04bf5481e6d5500cb780f6e98db8bbd15a28c"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 EPUBCheck")
    parser.add_argument("--force", action="store_true", help="已有也重新下载")
    args = parser.parse_args(argv)

    jar = next(TOOLS.rglob("epubcheck.jar"), None) if TOOLS.is_dir() else None
    if jar is not None and not args.force:
        print(f"已安装: {jar}")
        return 0

    TOOLS.mkdir(parents=True, exist_ok=True)
    zip_path = TOOLS / f"epubcheck-{VERSION}.zip"
    if args.force or not zip_path.is_file() or sha256(zip_path) != SHA256:
        print(f"下载 {URL}")
        with urllib.request.urlopen(URL, timeout=600) as resp, zip_path.open("wb") as out:
            shutil.copyfileobj(resp, out)
    actual = sha256(zip_path)
    if actual != SHA256:
        zip_path.unlink(missing_ok=True)
        print(f"校验失败: {actual} != {SHA256}", file=sys.stderr)
        return 2
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(TOOLS)
    jar = next(TOOLS.rglob("epubcheck.jar"), None)
    if jar is None:
        print("压缩包里没有 epubcheck.jar", file=sys.stderr)
        return 2
    print(f"EPUBCheck {VERSION}: {jar}")
    print("需要 Java 11+;引擎在 verify.epubcheck=auto/on 时会自动调用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
