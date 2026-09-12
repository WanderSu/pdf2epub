"""路径解析测试(计划 1.5):配置定位不依赖进程 cwd。

重点覆盖打包绿色版场景:壳用绝对路径调用 exe(任意 cwd),
引擎必须仍能从 **exe 同级 config/** 取到 book.css / config.yaml。
"""
from __future__ import annotations

import sys
from pathlib import Path

import paths
from conftest import PROJECT_ROOT


def _make_config(root: Path) -> Path:
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text("ocr_backend: mineru\n", encoding="utf-8")
    (cfg / "book.css").write_text("body{}\n", encoding="utf-8")
    return cfg


def test_env_var_has_priority(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    cfg = _make_config(home)
    monkeypatch.setenv("PDF2EPUB_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert paths.config_dir() == cfg


def test_cwd_config_used(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PDF2EPUB_HOME", raising=False)
    cfg = _make_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert paths.config_dir() == cfg


def test_exe_sibling_config_used_in_frozen_app(tmp_path: Path, monkeypatch) -> None:
    """打包运行:壳以任意 cwd 调用 → 仍取 exe 同级的 config/。"""
    monkeypatch.delenv("PDF2EPUB_HOME", raising=False)
    exe_dir = tmp_path / "green"
    cfg = _make_config(exe_dir)
    (exe_dir / "cli.exe").write_bytes(b"")
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "cli.exe"))
    assert paths.exe_dir() == exe_dir.resolve()
    assert paths.config_dir() == cfg
    assert paths.book_css() == cfg / "book.css"


def test_module_root_is_last_resort(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PDF2EPUB_HOME", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    # 源码开发模式:回落到项目根的 config/
    assert paths.config_dir() == PROJECT_ROOT / "config"


def test_api_key_found_next_to_exe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PDF2EPUB_HOME", raising=False)
    exe_dir = tmp_path / "green"
    exe_dir.mkdir()
    key = exe_dir / "apikey.json"
    key.write_text('{"MinerU": "token-from-file"}', encoding="utf-8")
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "cli.exe"))
    assert paths.api_key_file() == key
    assert paths.load_api_key("MinerU") == "token-from-file"


def test_load_api_key_missing_returns_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "_roots", lambda: [tmp_path])
    assert paths.api_key_file() is None
    assert paths.load_api_key("MinerU") is None
