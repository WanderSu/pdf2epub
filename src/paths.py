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

import os
import sys
import json
from pathlib import Path

API_KEY_FILE = "apikey.json"


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


def api_key_file() -> Path | None:
    """本地凭证文件 apikey.json(已 gitignore,不入库)。"""
    for root in _roots():
        p = root / API_KEY_FILE
        if p.exists():
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
