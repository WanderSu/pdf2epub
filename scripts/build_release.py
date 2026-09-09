"""一键出发布包(计划 1.4)。

    uv run python scripts/build_release.py 0.2.4                 # 构建 + 打包 + 自检
    uv run python scripts/build_release.py 0.2.4 --skip-build    # 复用已有产物,只打包
    uv run python scripts/build_release.py 0.2.4 --no-check      # 不做发布包自检

产物: dist_release/pdf2epub-v<版本>-win-x64.zip
    内含 pdf2epub.exe(壳) + cli.exe(引擎) + config/ + README.md

**版本守卫**:四处版本号(pyproject / tauri.conf.json / Cargo.toml / App.tsx)必须与
传入版本一致,否则拒绝打包 —— 防止「界面写着 v0.2.4、实际是 v0.2.3 的产物」。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bump_version import check_versions, read_versions   # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_CLI = PROJECT_ROOT / "dist_cli"
BUILD_CLI = PROJECT_ROOT / "build_cli"
DIST_RELEASE = PROJECT_ROOT / "dist_release"
DESKTOP = PROJECT_ROOT / "desktop"
TAURI_RELEASE = DESKTOP / "src-tauri" / "target" / "release"

#: 发布包内需要带上桌面的运行时配置(与源码 config/ 同源)
CONFIG_FILES = ("config.yaml", "book.css")

CLI_EXE_NAME = "cli.exe"
SHELL_EXE_NAME = "pdf2epub.exe"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"$ {' '.join(cmd)}" + (f"   (cwd={cwd})" if cwd else ""))
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise SystemExit(f"命令失败(exit={proc.returncode}): {' '.join(cmd)}")


def build_cli() -> Path:
    """PyInstaller 构建引擎 cli.exe(直接读源码,见 cli.spec)。"""
    run([sys.executable, "-m", "PyInstaller", "--noconfirm",
         "--distpath", str(DIST_CLI), "--workpath", str(BUILD_CLI),
         str(PROJECT_ROOT / "cli.spec")], cwd=PROJECT_ROOT)
    exe = DIST_CLI / CLI_EXE_NAME
    if not exe.exists():
        raise SystemExit(f"未生成 {exe}")
    return exe


def cargo_binary() -> Path:
    """桌面壳在 target/release 下的实际文件名(= Cargo [package] name)。"""
    import tomllib

    data = tomllib.loads((DESKTOP / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))
    name = (data.get("package") or {}).get("name", "desktop")
    return TAURI_RELEASE / f"{name}.exe"


def shell_exe() -> Path:
    """定位桌面壳产物。

    Tauri 2 产出的文件名是 Cargo crate 名(如 desktop.exe),而发布包统一叫
    pdf2epub.exe —— 这里负责找到它,`stage()` 负责重命名。
    """
    exe = cargo_binary()
    if exe.exists():
        return exe
    candidates = [p for p in TAURI_RELEASE.glob("*.exe") if p.is_file()]
    if not candidates:
        raise SystemExit(
            f"未找到桌面壳产物: {TAURI_RELEASE} 下没有 .exe\n"
            "先执行: cd desktop && npm run tauri build -- --no-bundle"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def build_shell() -> Path:
    """Tauri 构建桌面壳(绿色版 exe,不打安装包)。"""
    npm = shutil.which("npm") or "npm.cmd"
    run([npm, "run", "tauri", "build", "--", "--no-bundle"], cwd=DESKTOP)
    return shell_exe()


def stage(version: str, skip_build: bool) -> Path:
    """把产物收集到 dist_release/v<版本>/ 下。"""
    cli_exe = DIST_CLI / CLI_EXE_NAME if skip_build else build_cli()
    shell = shell_exe() if skip_build else build_shell()
    for exe in (cli_exe, shell):
        if not exe.exists():
            raise SystemExit(f"缺少产物: {exe}(先构建,或去掉 --skip-build)")

    staging = DIST_RELEASE / f"v{version}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copy2(shell, staging / SHELL_EXE_NAME)   # desktop.exe → pdf2epub.exe
    shutil.copy2(cli_exe, staging / CLI_EXE_NAME)

    cfg_dir = staging / "config"
    cfg_dir.mkdir()
    for name in CONFIG_FILES:
        src = PROJECT_ROOT / "config" / name
        if not src.exists():
            raise SystemExit(f"缺少配置文件: {src}")
        shutil.copy2(src, cfg_dir / name)

    readme = PROJECT_ROOT / "README.md"
    if readme.exists():
        shutil.copy2(readme, staging / "README.md")
    return staging


def make_zip(staging: Path, version: str) -> Path:
    zip_path = DIST_RELEASE / f"pdf2epub-v{version}-win-x64.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging).as_posix())
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pdf2epub 发布打包")
    parser.add_argument("version", help="版本号,如 0.2.4")
    parser.add_argument("--skip-build", action="store_true",
                        help="复用现有 dist_cli/cli.exe 与桌面 release exe,只重新打包")
    parser.add_argument("--no-check", action="store_true", help="跳过发布包自检")
    args = parser.parse_args(argv)

    # ---- 版本守卫 ----
    ok, versions = check_versions()
    mismatched = {rel: v for rel, v in versions.items() if v != args.version}
    if mismatched:
        print("版本守卫未通过:代码里的版本与要发布的版本不一致", file=sys.stderr)
        for rel, v in versions.items():
            flag = "OK " if v == args.version else "✗  "
            print(f"  {flag}{v:<12} {rel}", file=sys.stderr)
        print(f"\n先运行: uv run python scripts/bump_version.py {args.version}", file=sys.stderr)
        return 2
    if not ok:
        print("警告: 四处版本一致但与目标版本相同 —— 继续", file=sys.stderr)

    staging = stage(args.version, args.skip_build)
    zip_path = make_zip(staging, args.version)
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"\n发布包: {zip_path}  ({size_mb:.1f} MB)")
    for path in sorted(staging.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(staging).as_posix():<24} "
                  f"{path.stat().st_size / 1024 / 1024:7.1f} MB")

    if args.no_check:
        return 0

    # ---- 自检:解压 → 实跑引擎 → 结构校验 ----
    from check_release import check   # 延迟导入,避免循环依赖

    return check(zip_path)


if __name__ == "__main__":
    sys.exit(main())
