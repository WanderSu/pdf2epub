"""构建 Windows 安装包(NSIS)。

    uv run python scripts/build_installer.py 0.6.2
    uv run python scripts/build_installer.py 0.6.2 --verify-install
    uv run python scripts/build_installer.py 0.6.2 --mode machine   # 需要管理员权限编译

输入:``dist_release/v<版本>/``(由 ``scripts/build_release.py <版本> --skip-build`` 生成,
里面是 ``pdf2epub.exe`` + ``cli/`` + ``config/`` + ``README.md``)。
输出:``dist_release/pdf2epub-v<版本>-win-x64-setup.exe``。

**只打安装器,不搬数据**:用户数据在 ``%LOCALAPPDATA%\\pdf2epub\\``,卸载不删
(见 docs/windows-install.md)。

NSIS 工具链查找顺序:``$MAKENSIS`` → PATH → ``.tools/nsis/**/makensis.exe`` →
下载官方 zip 到 ``.tools/``(走 GitHub 镜像 ``tauri-apps/binary-releases``,
sha256 钉死;官方 SourceForge 在这台机器上实测只有 3 KB/s)。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = PROJECT_ROOT / "installer"
DIST_RELEASE = PROJECT_ROOT / "dist_release"
TOOLS_DIR = PROJECT_ROOT / ".tools"

#: NSIS 3.11(与 Tauri 打包用的一致);镜像走 GitHub,官方 SourceForge 太慢。
NSIS_URL = ("https://github.com/tauri-apps/binary-releases/releases/download/"
            "nsis-3.11/nsis-3.11.zip")
NSIS_SHA256 = "c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1"

#: 安装器里必备的文件(与 check_release.REQUIRED 对齐,外加卸载器由 NSIS 生成)
REQUIRED = ("pdf2epub.exe", "cli/cli.exe", "config/config.yaml", "config/book.css",
            "README.md")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_nsis() -> Path:
    """下载并解压 NSIS 到 ``.tools/``,返回 makensis 路径。"""
    zip_path = TOOLS_DIR / "nsis-3.11.zip"
    if not zip_path.is_file() or _sha256(zip_path) != NSIS_SHA256:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"下载 NSIS: {NSIS_URL}")
        with urllib.request.urlopen(NSIS_URL, timeout=120) as resp, \
                zip_path.open("wb") as out:
            shutil.copyfileobj(resp, out)
        actual = _sha256(zip_path)
        if actual != NSIS_SHA256:
            zip_path.unlink(missing_ok=True)
            raise SystemExit(f"NSIS 校验失败: {actual} != {NSIS_SHA256}")
    dest = TOOLS_DIR / "nsis"
    if not dest.is_dir():
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
    exe = next(dest.rglob("makensis.exe"), None)
    if exe is None:
        raise SystemExit(f"NSIS 包里没有 makensis.exe: {dest}")
    return exe


def find_makensis(allow_download: bool = True) -> Path:
    env = os.environ.get("MAKENSIS")
    if env and Path(env).is_file():
        return Path(env)
    found = shutil.which("makensis")
    if found:
        return Path(found)
    local = next(TOOLS_DIR.rglob("makensis.exe"), None) if TOOLS_DIR.is_dir() else None
    if local is not None:
        return local
    if not allow_download:
        raise SystemExit("未找到 makensis:请安装 NSIS、设置 MAKENSIS,或去掉 --no-download")
    return download_nsis()


def check_staging(src: Path) -> None:
    missing = [rel for rel in REQUIRED if not (src / rel).is_file()]
    if missing:
        raise SystemExit(
            f"{src} 缺文件: {', '.join(missing)}\n"
            f"先跑: uv run python scripts/build_release.py <版本> --skip-build"
        )
    if not (src / "cli" / "_internal").is_dir():
        raise SystemExit(f"{src / 'cli'} 不是 onedir 产物(缺 _internal/) —— 检查 cli.spec")


def write_generated(version: str, src: Path, outfile: Path, per_user: bool) -> Path:
    """写 ``installer/_generated.nsh``。

    用 include 而不是命令行 /D:路径里可能有空格(用户把仓库放在带空格的目录),
    命令行 define 的引号规则很容易出错。
    """
    gen = INSTALLER_DIR / "_generated.nsh"
    lines = [
        f'!define VERSION "{version}"',
        f'!define SRCDIR "{src}"',
        f'!define OUTFILE "{outfile}"',
        f'!define PER_USER {1 if per_user else 0}',
        "",
    ]
    # NSIS 3 要求脚本是 UTF-8 **带 BOM**(否则报 "Bad text encoding"),include 同理
    gen.write_text("\n".join(lines), encoding="utf-8-sig")
    return gen


def ensure_bom(path: Path) -> None:
    """NSIS 3 只认「UTF-8 带 BOM」或 UTF-16LE 的脚本(否则报 Bad text encoding)。

    编辑器/补丁工具经常把 BOM 抹掉,所以编译前统一补一次,而不是指望文件一直带着。
    """
    data = path.read_bytes()
    if not data.startswith(b"\xef\xbb\xbf"):
        path.write_bytes(b"\xef\xbb\xbf" + data)


def compile_installer(makensis: Path) -> None:
    ensure_bom(INSTALLER_DIR / "installer.nsi")
    ensure_bom(INSTALLER_DIR / "_generated.nsh")
    proc = subprocess.run([str(makensis), "/V2", "installer.nsi"], cwd=INSTALLER_DIR)
    if proc.returncode != 0:
        raise SystemExit(f"makensis 失败(exit={proc.returncode})")


def file_version(exe: Path) -> str:
    """读 exe 的 FileVersion(NSIS 的 VIProductVersion 写的是四段版本)。"""
    ps = ("(Get-Item -LiteralPath '" + str(exe).replace("'", "''") +
          "').VersionInfo.FileVersion")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    return out.stdout.strip()


def verify_install(setup: Path) -> None:
    """静默装到临时目录 → 查文件 → 跑引擎 → 静默卸载 → 确认目录消失。

    **不动真实安装位置**:``/D`` 指定临时目录;卸载器只删这个目录与 HKCU 卸载项,
    用户数据(``%LOCALAPPDATA%\\pdf2epub``)不会被碰。
    """
    target = Path(os.environ.get("TEMP", ".")) / f"p2e-install-{os.getpid()}"
    if target.exists():
        shutil.rmtree(target)
    print(f"静默安装到 {target} ...")
    subprocess.run([str(setup), "/S", f"/D={target}"], check=True, timeout=300)
    try:
        _check_installed(target)
    except BaseException:
        # 检查失败也要清场:否则临时目录和 HKCU 卸载项会留在机器上
        _silent_uninstall(target)
        raise

    print("静默卸载 ...")
    _silent_uninstall(target)
    print("  安装 → 运行引擎 → 卸载:通过")


def _check_installed(target: Path) -> None:
    # NSIS 安装器是同步的:返回时文件已经就位
    for rel in REQUIRED:
        if not (target / rel).is_file():
            raise SystemExit(f"安装后缺文件: {rel}")
    if not (target / "Uninstall.exe").is_file():
        raise SystemExit("安装后没有 Uninstall.exe")

    engine = target / "cli" / "cli.exe"
    out = subprocess.run([str(engine), "--help"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or "usage:" not in (out.stdout + out.stderr):
        raise SystemExit(f"安装后的引擎跑不起来: exit={out.returncode}")


def _silent_uninstall(target: Path) -> None:
    """静默卸载,并等目录真正消失(卸载器会把自己复制到 %TEMP% 里异步执行)。"""
    uninst = target / "Uninstall.exe"
    if uninst.is_file():
        subprocess.run([str(uninst), "/S"], check=False, timeout=120)
    for _ in range(40):
        if not target.exists():
            return
        time.sleep(0.5)
    if target.exists():
        # 卸载器失败(文件被占用等):删掉残留目录,别让测试把机器弄脏
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            raise SystemExit(f"卸载后目录仍在: {target}(可能有文件被占用)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 pdf2epub 的 Windows 安装包")
    parser.add_argument("version", help="版本号(必须与代码里的版本一致,如 0.6.2)")
    parser.add_argument("--src", default=None, help="发布目录(默认 dist_release/v<版本>)")
    parser.add_argument("--out", default=None, help="安装包输出路径")
    parser.add_argument("--mode", choices=["user", "machine"], default="user",
                        help="user = 装到 %%LOCALAPPDATA%%(默认,不弹 UAC);machine = Program Files")
    parser.add_argument("--no-download", action="store_true", help="缺 NSIS 时不自动下载")
    parser.add_argument("--verify-install", action="store_true",
                        help="装到临时目录 → 跑引擎 → 卸载(会写 HKCU 卸载项,卸载时删除)")
    args = parser.parse_args(argv)

    # 版本守卫:安装包文件名/注册表版本必须与代码一致(与 build_release 同一口径)
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from bump_version import check_versions

    ok, versions = check_versions()
    mismatched = {rel: v for rel, v in versions.items() if v != args.version}
    if mismatched:
        print("版本守卫未通过:", file=sys.stderr)
        for rel, v in versions.items():
            print(f"  {'OK ' if v == args.version else '✗  '}{v:<12} {rel}", file=sys.stderr)
        return 2

    src = Path(args.src) if args.src else DIST_RELEASE / f"v{args.version}"
    src = src.resolve()
    check_staging(src)
    outfile = (Path(args.out) if args.out else
               DIST_RELEASE / f"pdf2epub-v{args.version}-win-x64-setup.exe").resolve()
    outfile.parent.mkdir(parents=True, exist_ok=True)
    if outfile.exists():
        outfile.unlink()

    makensis = find_makensis(allow_download=not args.no_download)
    print(f"makensis: {makensis}")
    write_generated(args.version, src, outfile, per_user=args.mode == "user")
    compile_installer(makensis)

    if not outfile.is_file():
        raise SystemExit(f"没有生成安装包: {outfile}")
    size_mb = outfile.stat().st_size / 1024 / 1024
    if size_mb < 30:
        raise SystemExit(f"安装包只有 {size_mb:.1f} MB,像是漏打了 cli/ 目录")
    ver = file_version(outfile)
    print(f"\n安装包: {outfile}")
    print(f"  体积: {size_mb:.1f} MB   文件版本: {ver}  模式: {args.mode}")
    print(f"  sha256: {_sha256(outfile)}")

    if args.verify_install:
        if args.mode != "user":
            print("!! 跳过 --verify-install:per-machine 安装需要管理员权限", file=sys.stderr)
        else:
            verify_install(outfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
