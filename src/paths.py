"""项目路径解析(兼容源码运行与 pip 安装后运行)。

查找顺序:
  1. 环境变量 PDF2EPUB_HOME(项目根)
  2. 当前工作目录下的 config/(CLI 在项目根运行)
  3. 模块相对位置(源码开发模式)
"""
from __future__ import annotations

import os
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
