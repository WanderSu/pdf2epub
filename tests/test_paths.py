"""路径解析测试(计划 1.5):配置定位不依赖进程 cwd。

重点覆盖打包绿色版场景:壳用绝对路径调用 exe(任意 cwd),
引擎必须仍能从 **exe 同级 config/** 取到 book.css / config.yaml。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

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


# ---------- 凭证文件权限加固 ----------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 路径")
def test_tighten_windows_removes_inherited_acls(tmp_path: Path) -> None:
    """收紧后:继承 ACE 与 Users/Everyone 均不应留在 ACL 里。"""
    key = tmp_path / "apikey.json"
    key.write_text('{"MinerU": "t"}', encoding="utf-8")
    assert paths.tighten_api_key_permissions(key) is True

    out = paths.subprocess.run(
        ["icacls", str(key)], capture_output=True, creationflags=0x08000000, check=False
    ).stdout.decode("utf-8", "replace")
    # 先去掉文件路径本身——它恰好含 Users 目录名,会干扰断言
    acl = out.replace(str(key), "").lower()
    assert os.environ.get("USERNAME", "").lower() in acl
    assert "everyone" not in acl
    assert "builtin\\users" not in acl
    assert "authenticated users" not in acl
    # 加固不影响本机读取
    assert key.read_text(encoding="utf-8") == '{"MinerU": "t"}'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位路径")
def test_tighten_posix_chmods_600(tmp_path: Path) -> None:
    key = tmp_path / "apikey.json"
    key.write_text('{"MinerU": "t"}', encoding="utf-8")
    key.chmod(0o644)
    assert paths.tighten_api_key_permissions(key) is True
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_api_key_file_hardens_once(tmp_path: Path, monkeypatch) -> None:
    """读取凭证时自动加固,且同一次运行内不重复调用。"""
    calls: list[Path] = []
    monkeypatch.setattr(paths, "tighten_api_key_permissions", lambda p: calls.append(p) or True)
    monkeypatch.setattr(paths, "_HARDENED", set())
    key = tmp_path / "apikey.json"
    key.write_text('{"MinerU": "t"}', encoding="utf-8")
    monkeypatch.setattr(paths, "_roots", lambda: [tmp_path])

    assert paths.api_key_file() == key
    assert paths.api_key_file() == key
    assert calls == [key]


def test_perms_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    """PDF2EPUB_APIKEY_PERMS=0 时不再自动加固(多账号共享场景)。"""
    monkeypatch.setenv("PDF2EPUB_APIKEY_PERMS", "0")
    monkeypatch.setattr(paths, "_HARDENED", set())
    key = tmp_path / "apikey.json"
    key.write_text('{"MinerU": "t"}', encoding="utf-8")
    monkeypatch.setattr(paths, "_roots", lambda: [tmp_path])
    seen: list[Path] = []
    monkeypatch.setattr(paths, "tighten_api_key_permissions", lambda p: seen.append(p) or True)

    assert paths.api_key_file() == key
    assert seen == []
