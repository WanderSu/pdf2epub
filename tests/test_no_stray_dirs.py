"""工具脚本不许往仓库外写东西(计划外目录落盘守卫)。

背景:真实踩过两次坑 ——
  1. desktop/scripts/shot-readme.mjs 的输出目录默认写成 "../../docs",
     成立与否取决于从哪个目录启动,结果跑到了**仓库之外**(在项目盘的根下
     多出一个 docs 目录)。
  2. 复核发布包时用了 `gh release download --dir /tmp/rel-verify`。
     gh 是原生程序,不认 MSYS 的 /tmp 翻译,Windows 上把 /tmp 当成当前盘符
     根下的 tmp 目录,于是又在仓库外建了一个。

(上面两处原本记的是当时的具体盘符路径;那是本机信息,这里只保留成因。)

这两个都靠"改注释提醒自己"是防不住的,所以这里用测试把纪律钉住:
  - 工具脚本里不许出现裸的 /tmp / %TEMP% 之类字面量;
  - 脚本的产物路径必须由自身位置推算,不许用 cwd 相对路径;
  - 任何"写文件"的目录都得先过一道断言,确认没跑出允许的根。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT

DESKTOP_SCRIPTS = PROJECT_ROOT / "desktop" / "scripts"
PY_SCRIPTS = PROJECT_ROOT / "scripts"

# 会被当成"临时/输出根"的危险字面量
TMP_LITERALS = ('"/tmp', "'/tmp", "/tmp/", '"C:\\\\tmp', "E:\\\\tmp")


def _js_scripts() -> list[Path]:
    if not DESKTOP_SCRIPTS.is_dir():
        pytest.skip("没有 desktop/scripts(例如源码包未包含桌面端)")
    files = sorted(DESKTOP_SCRIPTS.glob("*.mjs"))
    if not files:
        pytest.skip("desktop/scripts 下没有 .mjs")
    return files


@pytest.mark.parametrize("script", _js_scripts(), ids=lambda p: p.name)
def test_js_script_has_no_tmp_literal(script: Path) -> None:
    """不许出现 /tmp 这类字面量:落点取决于 shell 挂载,可能跑到项目外。"""
    text = script.read_text(encoding="utf-8")
    for lit in TMP_LITERALS:
        assert lit not in text, f"{script.name} 出现临时目录字面量 {lit}"


@pytest.mark.parametrize("script", _js_scripts(), ids=lambda p: p.name)
def test_js_script_anchors_output_to_itself(script: Path) -> None:
    """产物目录必须由脚本自身位置推算(import.meta.url),不能靠 cwd。"""
    text = script.read_text(encoding="utf-8")
    if "writeFileSync" not in text:
        pytest.skip(f"{script.name} 不写文件")
    assert "import.meta.url" in text, (
        f"{script.name} 写文件却没有用 import.meta.url 定位目录 —— "
        "相对 cwd 的路径换个启动目录就会解析到仓库外"
    )
    assert "fileURLToPath" in text, f"{script.name} 应把 import.meta.url 转成真实路径"


@pytest.mark.parametrize("script", _js_scripts(), ids=lambda p: p.name)
def test_js_script_guards_writes(script: Path) -> None:
    """写文件前要过 safeOut 之类的越界断言,不能直接 join(OUT, name) 就写。"""
    text = script.read_text(encoding="utf-8")
    if "writeFileSync" not in text:
        pytest.skip(f"{script.name} 不写文件")
    assert "safeOut" in text, f"{script.name} 缺少写入前的路径越界断言"


def test_python_scripts_use_tempfile_not_tmp_literal() -> None:
    """Python 侧要临时目录就用 tempfile(落 %TEMP%),别手写 /tmp。"""
    offenders = []
    for script in sorted(PY_SCRIPTS.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue  # 注释里提到 /tmp 是在解释为什么不用它
            if re.search(r"[\"']/tmp[/\"']", line):
                offenders.append(f"{script.name}: {line.strip()}")
    assert not offenders, "以下位置手写了 /tmp:\n" + "\n".join(offenders)


def test_js_scripts_do_not_use_cwd() -> None:
    """脚本里不读 process.cwd(),避免"在哪启动就在哪落盘"。"""
    offenders = []
    for script in _js_scripts():
        text = script.read_text(encoding="utf-8")
        if "process.cwd()" in text:
            offenders.append(script.name)
    assert not offenders, f"这些脚本用 process.cwd() 定位路径: {offenders}"
