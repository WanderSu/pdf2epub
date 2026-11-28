r"""项目路径解析(兼容源码运行与打包后运行)。

config/ 查找顺序:
  1. 环境变量 PDF2EPUB_HOME(项目根)
  2. 当前工作目录下的 config/(CLI 在项目根运行)
  3. **exe 同级 config/**(旧绿色版:解压目录即根)
  4. **exe 上级 config/**(v0.6+ onedir 布局:引擎在 cli/ 子目录,config/ 在安装根)
  5. 用户数据目录(`%LOCALAPPDATA%\pdf2epub`,桌面端新布局)
  6. 模块相对位置(源码开发模式)

apikey.json 同理(工作目录 → exe 同级 → exe 上级 → 用户数据目录 → 项目根),
与桌面端 Rust 侧的候选顺序保持一致,避免「设置页保存了凭证、引擎却读不到」。
**用户数据目录这一条是必须的**:桌面端 0.6.2 起把凭证写在 `%LOCALAPPDATA%\pdf2epub`
(安装目录可能只读),壳调用引擎时 cwd 是安装根 —— 少了它,设置页显示「已配置」,
转换却报「缺少 Token」(装到 Program Files 后必然踩到)。
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

    v0.6+ onedir 布局把引擎放在安装根的 `cli/` 子目录(`pdf2epub.exe`(壳)、
    `cli/cli.exe`(引擎)、`config/` 在安装根),而引擎可能被壳以任意 cwd 调用
    (绝对路径),所以必须按 exe 位置找配置 —— 同级(旧布局)与上级(新布局)
    都要认,升级用户解压覆盖后两种布局可能短暂共存。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def _module_root() -> Path:
    """源码开发模式下的项目根(``src/pdf2epub/paths.py`` → 上两级 = 仓库根)。"""
    return Path(__file__).resolve().parents[2]


def data_root() -> Path | None:
    r"""桌面端的用户数据目录(`%LOCALAPPDATA%\pdf2epub`)。

    优先读 ``PDF2EPUB_DATA``(测试/便携部署可覆盖),否则 Windows 用 LOCALAPPDATA。
    其他平台没有这个约定时返回 None —— 命令行用户把 apikey.json 放在 cwd 即可。
    """
    env = os.environ.get("PDF2EPUB_DATA")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "pdf2epub") if local else None


def _roots() -> list[Path]:
    """按优先级排列的候选根目录(cwd → exe 同级 → exe 上级 → 源码根)。

    exe 上级只在冻结运行时加入:源码模式下 `src/` 的上级也常常是各种
    无关目录,加进去只会让「碰巧有 config/ 的父目录」劫持定位。
    """
    roots: list[Path] = [Path.cwd()]
    exe = exe_dir()
    if exe is not None:
        roots.append(exe)
        roots.append(exe.parent)
    data = data_root()
    if data is not None:
        roots.append(data)
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
    # 属主给 (F) 而不是 (R,W):凭证是用户自己的文件,只有读写权限的话
    # 连删除/改名/迁移都做不了(实测:删除旧安装目录时 apikey.json 报 Access denied,
    # 阶段 1b 的搬家同样会失败)。「仅属主可访问」的语义由 /inheritance:r 保证。
    grant = f"{principal}:(F)"
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


def _windows_permissions_ok(target: Path) -> bool:
    """Windows:用 icacls 复核 —— 不应再有任何继承条目(I),且当前用户仍有权限。

    `/inheritance:r` 之后 ACL 里只该剩下显式条目;若还能读到 `(I)` 行,说明收紧
    没生效(文件在只读介质、策略拦截,或 icacls 不可用)。解析失败一律按"未确认"
    处理,交给调用方告警,而不是假装成功。
    """
    user = os.environ.get("USERNAME", "").strip()
    if not user:
        return False
    creation_flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        done = subprocess.run(
            ["icacls", str(target)],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, ValueError):
        return False
    if done.returncode != 0:
        return False
    out = done.stdout or ""
    if "(I)" in out:
        return False
    return user.lower() in out.lower()


def verify_api_key_permissions(target: Path | str) -> bool:
    """复核凭证文件是否已是"仅当前用户可读写"。

    与 `tighten_api_key_permissions` 的分工:那个是"尽力收紧",这个是"收紧之后
    是否真的生效" —— 便携版在只读介质、组策略限制、icacls 缺失时都可能失败,
    读凭证的程序应当把失败说出来,而不是静默按最宽松的权限继续。
    """
    p = Path(target)
    if not p.is_file():
        return False
    if sys.platform == "win32":
        return _windows_permissions_ok(p)
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
    except OSError:
        return False
    return not (mode & ~API_KEY_MODE)


def redact_secret(text: str, *secrets: str) -> str:
    """把凭证值从任意文本里抹掉(日志、异常消息、事件正文共用)。

    只处理长度 ≥8 的值:太短的值(测试用的 "t" 之类)替换会误伤正常文本,
    而真实 Token 远长于 8 位。
    """
    out = text
    for value in secrets:
        if value and len(value) >= 8:
            out = out.replace(value, "***")
    return out


def _auto_harden(target: Path) -> None:
    """读取前顺手加固:每次运行只会触发一次;收紧后再复核一次。"""
    if not perms_lock_enabled():
        return
    key = str(target)
    if key in _HARDENED:
        return
    _HARDENED.add(key)
    tighten_api_key_permissions(target)
    if verify_api_key_permissions(target):
        return
    message = (f"凭证文件权限未能收紧到「仅当前用户可读写」: {target};"
               f" 若该文件被其他账户读取,请手动检查(Windows 用 icacls,其他平台 chmod 600)")
    print(f"[paths] ⚠ {message}", file=sys.stderr)
    try:
        from pdf2epub import events
        events.emit("warning", code="apikey_permissions", message=message)
    except Exception:  # noqa: BLE001 - 告警通道不可用时不能反过来影响读凭证
        pass


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
