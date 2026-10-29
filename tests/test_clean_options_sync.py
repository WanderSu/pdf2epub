"""清理开关「三处同源」的守卫(默认值与键序)。

同一组开关存在于三个地方:引擎的 `CleanOptions`(唯一来源)、`config.yaml` 的
`clean:` 段(用户改得到的出厂配置)、桌面端「规范」页(`desktop/src/types.ts` 的
`CLEAN_KEYS` / `CLEAN_DEFAULTS`)。三处长期靠人工对齐 —— 谁改一边忘了另一边,
测试照样全绿,症状是「同一本书在界面里和在命令行里清得不一样」,极难查。

这里钉住的是**默认值与键序**;运行时的覆盖规则(桌面端只透传「关掉的项」,
所以它只能否决、不能强开)见 docs/design.md §8。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from clean.options import CLEAN_KEYS, CleanOptions
from cli import main as cli_main
from conftest import make_pdf

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "config.yaml"
TYPES = ROOT / "desktop" / "src" / "types.ts"

DEFAULTS = CleanOptions().to_dict()


def _config_clean() -> dict[str, bool]:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return data["clean"]


def _ts_clean_keys() -> list[str]:
    text = TYPES.read_text(encoding="utf-8")
    block = re.search(r"export const CLEAN_KEYS = \[(.*?)\] as const;", text, re.S)
    assert block, "types.ts 里找不到 CLEAN_KEYS"
    return re.findall(r'"([^"]+)"', block.group(1))


def _ts_clean_defaults() -> dict[str, bool]:
    text = TYPES.read_text(encoding="utf-8")
    block = re.search(r"export const CLEAN_DEFAULTS: CleanOpts = \{(.*?)\n\};", text, re.S)
    assert block, "types.ts 里找不到 CLEAN_DEFAULTS"
    return {
        key: value == "true"
        for key, value in re.findall(r"(\w+):\s*(true|false)", block.group(1))
    }


def test_config_yaml_clean_section_matches_engine_defaults() -> None:
    """config.yaml 的 `clean:` 段必须与引擎默认值逐项相同,键集也要相同。

    它是发布包里唯一一份「出厂配置」:少一项意味着那个开关的默认值只在代码里,
    用户在配置文件里看不到也改不了;多一项或值不同则意味着同一次转换,命令行
    与界面会给出不同的清理结果。
    """
    cfg = _config_clean()
    assert set(cfg) == set(DEFAULTS), (
        f"键集不同: config.yaml={sorted(cfg)} 引擎={sorted(DEFAULTS)}"
    )
    for key in CLEAN_KEYS:
        assert cfg[key] is DEFAULTS[key], (
            f"{key}: config.yaml={cfg[key]} 引擎默认={DEFAULTS[key]}"
        )


def test_desktop_clean_keys_follow_the_engine_order() -> None:
    """桌面端的键序必须与引擎一致。

    键序本身不影响 `--clean-disable` 的正确性,但界面的分组顺序、清理报告的
    `run.options` 键序都按这份清单排;顺序分叉会让「同一项在两个地方看起来
    不是同一项」。
    """
    assert _ts_clean_keys() == list(CLEAN_KEYS)


def test_desktop_clean_defaults_match_the_engine() -> None:
    """桌面端手抄的那份默认值必须与引擎一致。

    不一致的表现不是报错,而是「某个开关在界面里显示为开、引擎里其实是关」
    (或反过来)—— 用户只会觉得清理结果不对,不会想到是两处默认值分叉。
    """
    assert _ts_clean_defaults() == DEFAULTS


def test_cli_clean_enable_flag_is_wired(tmp_path, capsys) -> None:
    """`--clean-enable` 必须真的接进 `resolve_options` 并打出来。

    只测 Python 层的 `resolve_options` 不够:参数名打错、或忘了往下透传,症状是
    「界面勾上没用、命令行也一样」—— 静默回到「只能关不能开」,而这正是配置里写死
    `false` 的开关在界面上勾选无效的原因。

    (不用 `--json-events`:那条路会把 stdout 换成机器通道,人类日志改道 stderr。)
    """
    pdf = make_pdf(tmp_path / "本地书.pdf")
    rc = cli_main([str(pdf), "--clean-enable", "bold", "--dry-run"])
    assert rc == 0
    assert "本次开启(覆盖配置): bold" in capsys.readouterr().out
