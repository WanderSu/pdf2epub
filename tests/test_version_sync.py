"""版本同步与打包守卫测试(计划 1.4)。

在临时目录里复刻各处版本文件,验证 read/check/write 的行为(不碰真实仓库)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bump_version
from bump_version import TARGETS, check_versions, read_versions, write_versions

SAMPLE = {
    "pyproject.toml": '[project]\nname = "pdf2epub"\nversion = "0.2.3"\n',
    "uv.lock": '[[package]]\nname = "pdf2epub"\nversion = "0.2.3"\nsource = { editable = "." }\n',
    "desktop/src-tauri/tauri.conf.json": '{\n  "productName": "pdf2epub",\n  "version": "0.2.3",\n  "identifier": "com.x.y"\n}\n',
    "desktop/src-tauri/Cargo.toml": '[package]\nname = "desktop"\nversion = "0.2.3"\nedition = "2021"\n',
    "desktop/src/App.tsx": 'const APP_VERSION = "v0.2.3";\n',
    "README.md": "下载 `pdf2epub-v0.2.3-win-x64.zip` 并解压。\n",
    "README.en.md": "Grab `pdf2epub-v0.2.3-win-x64.zip` from Releases.\n",
}


def _make_tree(root: Path, version: str = "0.2.3") -> None:
    for rel, text in SAMPLE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace("0.2.3", version), encoding="utf-8")


def test_targets_cover_all_places() -> None:
    assert {rel for rel, _, _ in TARGETS} == set(SAMPLE)


def test_read_versions(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    assert set(read_versions(tmp_path).values()) == {"0.2.3"}


def test_check_versions_detects_mismatch(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nversion = "0.1.0"\n', encoding="utf-8")
    consistent, versions = check_versions(tmp_path)
    assert consistent is False
    assert versions["pyproject.toml"] == "0.1.0"


def test_write_versions_syncs_all(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    changes = write_versions("0.2.4", tmp_path)
    assert all(new == "0.2.4" for _, new in changes.values())
    consistent, versions = check_versions(tmp_path)
    assert consistent is True
    assert set(versions.values()) == {"0.2.4"}
    # App.tsx 保留 "v" 前缀
    assert 'APP_VERSION = "v0.2.4"' in (tmp_path / "desktop/src/App.tsx").read_text(encoding="utf-8")


def test_write_versions_preserves_crlf(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    target = tmp_path / "pyproject.toml"
    target.write_bytes(target.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    write_versions("0.2.4", tmp_path)
    assert b"\r\n" in target.read_bytes()
    assert b'\r\nversion = "0.2.4"\r\n' in target.read_bytes()


def test_write_versions_reports_missing_file(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    (tmp_path / "desktop/src/App.tsx").unlink()
    with pytest.raises(FileNotFoundError):
        write_versions("0.2.4", tmp_path)


def test_write_versions_raises_when_pattern_absent(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    with pytest.raises(ValueError):
        write_versions("0.2.4", tmp_path)


def test_missing_target_reported_as_absent(tmp_path: Path) -> None:
    versions = read_versions(tmp_path)
    assert set(versions.values()) == {"<缺失>"}


def test_uv_lock_version_is_synced_and_keeps_line_endings(tmp_path: Path) -> None:
    """uv.lock 里的 pdf2epub 版本也要同步,并保留该文件原有的换行风格。

    漏掉 uv.lock 时:pyproject 是 0.4.2、锁文件还写着 0.4.1,下一次 `uv run`
    就会把工作区改脏(uv 按 pyproject 重写这条记录)。
    """
    _make_tree(tmp_path)
    lock = tmp_path / "uv.lock"
    lock.write_bytes(lock.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    changes = write_versions("0.2.4", tmp_path)
    assert changes["uv.lock"] == ("0.2.3", "0.2.4")      # 报出的旧版本要取对捕获组
    data = lock.read_bytes()
    assert b'name = "pdf2epub"\r\nversion = "0.2.4"\r\n' in data
    assert data.count(b"\n") == data.count(b"\r\n")      # 没有混进裸 LF
    assert read_versions(tmp_path)["uv.lock"] == "0.2.4"


def test_uv_lock_only_touches_the_project_package(tmp_path: Path) -> None:
    """只改 pdf2epub 一条:依赖包各自有自己的版本号,不是同步对象。"""
    _make_tree(tmp_path)
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "anyio"\nversion = "0.2.3"\n\n'
        '[[package]]\nname = "pdf2epub"\nversion = "0.2.3"\nsource = { editable = "." }\n',
        encoding="utf-8")
    write_versions("0.2.4", tmp_path)
    text = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "anyio"\nversion = "0.2.3"' in text
    assert 'name = "pdf2epub"\nversion = "0.2.4"' in text


def test_repository_versions_are_consistent() -> None:
    """真实仓库的各处版本必须一致(发布前守卫)。"""
    consistent, versions = check_versions(bump_version.PROJECT_ROOT)
    assert consistent, versions
