"""项目路径解析(兼容源码运行与打包后运行)。

config/ 查找顺序:
  1. 环境变量 PDF2EPUB_HOME(项目根)
  2. 当前工作目录下的 config/(CLI 在项目根运行)
  3. **exe 同级 config/**(打包绿色版:解压目录即根,与发布包约定一致)
  4. 模块相对位置(源码开发模式)

apikey.json 同理(工作目录 → exe 同级 → 项目根),与桌面端 Rust 侧的
候选顺序保持一致,避免「设置页保存了凭证、引擎却读不到」。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

API_KEY_FILE = "apikey.json"

#: 凭证文件的目标权限:仅属主可读写(Windows 通过 ACL 表达同样的语义)
API_KEY_MODE = 0o600

#: 本次进程内已经收紧过的凭证文件,避免重复调用外部工具
_HARDENED: set[str] = set()


def exe_dir() -> Path | None:
    """冻结(PyInstaller/打包)运行时的 exe 所在目录;源码运行返回 None。

    绿色版把 `pdf2epub.exe`(壳)、`cli.exe`(引擎)、`config/` 放在同级,
    而引擎可能被壳以任意 cwd 调用(绝对路径),所以必须按 exe 位置找配置。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def _module_root() -> Path:
    """源码开发模式下的项目根(src/paths.py → 上一级)。"""
    return Path(__file__).resolve().parent.parent


def _roots() -> list[Path]:
    """按优先级排列的候选根目录(cwd → exe 同级 → 源码根)。"""
    roots: list[Path] = [Path.cwd()]
    exe = exe_dir()
    if exe is not None:
        roots.append(exe)
    roots.append(_module_root())
    return roots


def config_dir() -> Path:
    env = os.environ.get("PDF2EPUB_HOME")
    if env:
        p = Path(env) / "config"
        if p.exists():
            return p
    for root in _roots():
        p = root / "config"
        if p.exists():
            return p
    return Path.cwd() / "config"


def config_file() -> Path:
    return config_dir() / "config.yaml"


def book_css() -> Path:
    return config_dir() / "book.css"


def perms_lock_enabled() -> bool:
    """是否收紧凭证文件权限。

    默认开启;`PDF2EPUB_APIKEY_PERMS=0`(或 no/off/false)关闭——
    用于确需多账号共享同一份凭证文件的场景。
    """
    return os.environ.get("PDF2EPUB_APIKEY_PERMS", "1").strip().lower() not in {
        "0",
        "no",
        "off",
        "false",
    }


def _too_open_posix(target: Path) -> bool:
    """POSIX:属主以外仍留有任何权限位。"""
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        return False
    return bool(mode & ~API_KEY_MODE)


def _tighten_posix(target: Path) -> bool:
    try:
        target.chmod(API_KEY_MODE)
    except OSError:
        return False
    return True


def _tighten_windows(target: Path) -> bool:
    """Windows:移除继承的 ACE,只给当前用户读写权。

    用系统自带的 icacls(所有受支持的 Windows 都可用),无需引入 pywin32。
    """
    user = os.environ.get("USERNAME", "").strip()
    if not user:
        return False
    domain = os.environ.get("USERDOMAIN", "").strip()
    principal = f"{domain}\\{user}" if domain else user
    grant = f"{principal}:(R,W)"
    creation_flags = 0x08000000  # CREATE_NO_WINDOW:不闪黑窗
    try:
        # /inheritance:r 去掉父目录继承来的条目,/grant:r 覆盖旧的显式条目
        done = subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, ValueError):
        return False
    return done.returncode == 0


def tighten_api_key_permissions(target: Path | str) -> bool:
    """把凭证文件收敛为「仅当前用户可读写」;已符合时不重复处理。

    Windows 收敛 ACL,POSIX chmod 600。失败一律返回 False,不影响凭证读取,
    属于「尽力的加固」而非强依赖——便携版在某些只读介质上加固不了是本分内的。
    """
    p = Path(target)
    if not p.is_file():
        return False
    if sys.platform == "win32":
        return _tighten_windows(p)
    if not _too_open_posix(p):
        return True
    return _tighten_posix(p)


def _auto_harden(target: Path) -> None:
    """读取前顺手加固:每次运行只会触发一次。"""
    if not perms_lock_enabled():
        return
    key = str(target)
    if key in _HARDENED:
        return
    _HARDENED.add(key)
    tighten_api_key_permissions(target)


def api_key_file() -> Path | None:
    """本地凭证文件 apikey.json(已 gitignore,不入库)。

    顺带把权限收敛到仅当前用户可读写(`tighten_api_key_permissions`)。
    """
    for root in _roots():
        p = root / API_KEY_FILE
        if p.exists():
            _auto_harden(p)
            return p
    return None


def load_api_key(service: str) -> str | None:
    """按服务名从 apikey.json 读取凭证(如 \"MinerU\" / \"PaddleOCR-VL\")。"""
    f = api_key_file()
    if f is None:
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        value = data.get(service)
        return str(value).strip() if value else None
    except (OSError, ValueError):
        return None
