"""语言检测(v0.3.2 P1-4):EPUB `dc:language` 不再写死 zh-CN。

纯统计、零依赖 —— 按 Unicode 脚本区间数正文,够用且可预测:

- 假名(平假名 / 片假名)→ 日语
- 谚文 → 韩语
- 汉字为主、无假名谚文 → 中文(zh-CN)
- 拉丁字母为主 → 英语(en)
- 样本太少或全是符号 → None,由调用方回退默认值

不做繁简判别(zh-CN/zh-TW)、不做语种置信度分级:宁可给 zh-CN 这个项目默认值,
也不要瞎猜一个看似精确的错误标签。显式覆盖(CLI `--lang` / 配置)优先级最高。
"""
from __future__ import annotations

import re

#: 判定所需的最少「有意义字符」数(低于此值不下结论)
MIN_SAMPLE_CHARS = 20
#: 假名 / 谚文占比达到此值即判定为相应语言(日语正文里假名通常占两三成,
#: 中文正文里为 0,所以门槛可以很低)
SCRIPT_RATIO = 0.02
SCRIPT_MIN_COUNT = 10
#: 汉字在「汉字 + 拉丁」里的占比门槛(中文正文绝大多数是汉字)
HAN_RATIO = 0.3
#: 拉丁字母占比门槛(英文正文绝大多数是拉丁字母)
LATIN_RATIO = 0.6

DEFAULT_LANGUAGE = "zh-CN"

KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")           # 平假名 + 片假名
HANGUL_RE = re.compile(r"[\u1100-\u11ff\uac00-\ud7af]")          # 谚文字母 + 音节
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")  # 汉字
LATIN_RE = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str | None:
    """按脚本占比判断语言;判断不出返回 None。"""
    sample = text[:50000]
    kana = len(KANA_RE.findall(sample))
    hangul = len(HANGUL_RE.findall(sample))
    han = len(HAN_RE.findall(sample))
    latin = len(LATIN_RE.findall(sample))

    meaningful = kana + hangul + han + latin
    if meaningful < MIN_SAMPLE_CHARS:
        return None

    if kana >= max(SCRIPT_MIN_COUNT, meaningful * SCRIPT_RATIO):
        return "ja"
    if hangul >= max(SCRIPT_MIN_COUNT, meaningful * SCRIPT_RATIO):
        return "ko"

    cjk_latin = han + latin
    if cjk_latin == 0:
        return None
    if han / cjk_latin >= HAN_RATIO:
        return "zh-CN"
    if latin / cjk_latin >= LATIN_RATIO:
        return "en"
    return None


def resolve_language(
    text: str,
    *,
    override: str | None = None,
    default: str = DEFAULT_LANGUAGE,
) -> str:
    """最终语言:显式覆盖 > 检测结果 > 默认值。"""
    if override:
        return override.strip() or default
    return detect_language(text) or default
