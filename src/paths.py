"""项目路径解析(兼容源码运行与 pip 安装后运行)。

查找顺序:
  1. 环境变量 PDF2EPUB_HOME(项目根)
  2. 当前工作目录下的 config/(CLI 在项目根运行)
  3. 模块相对位置(源码开发模式)
"""
from __future__ import annotations

import os
import json
from pathlib import Path


def config_dir() -> Path:
    env = os.environ.get("PDF2EPUB_HOME")
    if env:
        p = Path(env) / "config"
        if p.exists():
            return p
    if (Path.cwd() / "config").exists():
        return Path.cwd() / "config"
    p = Path(__file__).resolve().parent.parent.parent / "config"
    if p.exists():
        return p
    return Path.cwd() / "config"


def config_file() -> Path:
    return config_dir() / "config.yaml"


def book_css() -> Path:
    return config_dir() / "book.css"


API_KEY_FILE = "apikey.json"


def api_key_file() -> Path | None:
    """项目根下的 apikey.json(本地凭证文件,已 gitignore,不入库)。"""
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
        p = base / API_KEY_FILE
        if p.exists():
            return p
    return None


def load_api_key(service: str) -> str | None:
    """按服务名从 apikey.json 读取凭证(如 "MinerU" / "PaddleOCR-VL")。"""
    f = api_key_file()
    if f is None:
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        value = data.get(service)
        return str(value).strip() if value else None
    except (OSError, ValueError):
        return None
