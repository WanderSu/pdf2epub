"""版本号同步(计划 1.4)。

版本号散落在多处,手工同步容易漏 —— 本脚本是唯一入口:

    uv run python scripts/bump_version.py 0.2.4          # 写入
    uv run python scripts/bump_version.py --check        # 只检查是否一致

同步目标(6 处):
  - pyproject.toml               (Python 包版本)
  - desktop/src-tauri/tauri.conf.json (壳:安装包/窗口版本)
  - desktop/src-tauri/Cargo.toml (Rust crate 版本)
  - desktop/src/App.tsx          (界面左下角显示的 APP_VERSION)
  - README.md / README.en.md     (发布包文件名,避免下载引导指向旧版本)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: (相对路径, 版本号正则, 替换模板)。模板用 {v} 占位。
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("pyproject.toml",
     r'(?m)^version\s*=\s*"([^"]+)"',
     'version = "{v}"'),
    ("desktop/src-tauri/tauri.conf.json",
     r'"version"\s*:\s*"([^"]+)"',
     '"version": "{v}"'),
    ("desktop/src-tauri/Cargo.toml",
     r'(?m)^version\s*=\s*"([^"]+)"',
     'version = "{v}"'),
    ("desktop/src/App.tsx",
     r'APP_VERSION\s*=\s*"v([^"]+)"',
     'APP_VERSION = "v{v}"'),
    # README 里的发布包文件名(下载引导),不同步会指向旧版本
    ("README.md",
     r'pdf2epub-v(\d+\.\d+\.\d+)-win-x64\.zip',
     'pdf2epub-v{v}-win-x64.zip'),
    ("README.en.md",
     r'pdf2epub-v(\d+\.\d+\.\d+)-win-x64\.zip',
     'pdf2epub-v{v}-win-x64.zip'),
)

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def read_versions(root: Path = PROJECT_ROOT) -> dict[str, str]:
    """读取各处的当前版本号(文件缺失 → 值为 "<缺失>")。"""
    versions: dict[str, str] = {}
    for rel, pattern, _ in TARGETS:
        path = root / rel
        if not path.exists():
            versions[rel] = "<缺失>"
            continue
        m = re.search(pattern, path.read_text(encoding="utf-8"))
        versions[rel] = m.group(1) if m else "<未找到>"
    return versions


def check_versions(root: Path = PROJECT_ROOT) -> tuple[bool, dict[str, str]]:
    """四处版本是否一致。返回 (是否一致, 各文件版本)。"""
    versions = read_versions(root)
    return len(set(versions.values())) == 1, versions


def write_versions(new: str, root: Path = PROJECT_ROOT) -> dict[str, tuple[str, str]]:
    """写入版本号,返回 {文件: (旧版本, 新版本)}。

    按字节读/写,保留各文件原有的换行风格(工作区多为 CRLF),避免整文件 diff。
    """
    changes: dict[str, tuple[str, str]] = {}
    for rel, pattern, template in TARGETS:
        path = root / rel
        if not path.exists():
            raise FileNotFoundError(f"版本目标文件不存在: {path}")
        text = path.read_bytes().decode("utf-8")
        m = re.search(pattern, text)
        if m is None:
            raise ValueError(f"在 {rel} 中找不到版本号(正则不匹配,文件结构变了?)")
        old = m.group(1)
        if old == new:
            changes[rel] = (old, new)
            continue
        updated = text[:m.start()] + template.format(v=new) + text[m.end():]
        path.write_bytes(updated.encode("utf-8"))
        changes[rel] = (old, new)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 pdf2epub 各处版本号")
    parser.add_argument("version", nargs="?", help="新版本号,如 0.2.4")
    parser.add_argument("--check", action="store_true", help="只检查各处是否一致")
    args = parser.parse_args(argv)

    if args.check or not args.version:
        ok, versions = check_versions()
        for rel, v in versions.items():
            print(f"  {v:<12} {rel}")
        print("版本一致 ✓" if ok else "版本不一致 ✗(先运行 bump_version.py <版本> 同步)")
        return 0 if ok else 1

    if not SEMVER_RE.match(args.version):
        print(f"版本号格式无效: {args.version}(应形如 0.2.4)", file=sys.stderr)
        return 2

    before_ok, _ = check_versions()
    for rel, (old, new) in write_versions(args.version).items():
        mark = "=" if old == new else "→"
        print(f"  {old:<12} {mark} {new:<12} {rel}")
    print(f"版本已同步为 {args.version}" + ("" if before_ok else "(此前各处版本不一致)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
