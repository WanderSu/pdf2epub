"""清理开关(配置面):``clean:`` 段、``--clean-disable``、桌面端「清理选项」共用。

这个模块只描述**用户能关掉什么**,不包含任何清理逻辑。默认值的唯一来源就是这里的
``CleanOptions`` —— 报告(``clean-report.json`` 的 ``run.options``)、CLI 帮助、桌面端
选项列表都从它派生,不各存一份(复制一份迟早会有一边忘了改)。

``bold`` 是唯一一个"不作用于 Markdown 文本"的开关:它落到后端的字体配置
(``pymupdf.bold_fonts``),由 :meth:`CleanOptions.apply` 处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

#: 清理项开关名(**顺序即报告 ``run.options`` 的键序**,也是 CLI 帮助里的顺序)
CLEAN_KEYS = (
    "page_numbers",    # 剔除独立页码行
    "running_heads",   # 剔除页眉页脚重复行
    "join_lines",      # 跨页断行连接
    "ocr_spaces",      # OCR 异常空格合并
    "cjk_spaces",      # 中文排版空格修正
    "dup_headings",    # 相邻重复标题去重
    "headings",        # 空标题删除 + 层级修正
    "bold",            # 强调字体 → `**` 粗体(实际作用于 pymupdf 后端)
    "images",          # 图片引用存在性校验
)


@dataclass
class CleanOptions:
    """Markdown 清理项开关。

    默认值与 config.yaml 的 `clean:` 段、桌面端「清理选项」一致
    (bold 默认关:强调字体标注会让正文出现较多 `**`,需要时再开)。
    """

    page_numbers: bool = True   # 剔除独立页码行
    running_heads: bool = True  # 剔除页眉页脚重复行
    join_lines: bool = True     # 跨页断行连接
    ocr_spaces: bool = True     # OCR 异常空格合并
    cjk_spaces: bool = True     # 中文排版空格修正
    dup_headings: bool = True   # 相邻重复标题去重
    headings: bool = True       # 空标题删除 + 标题层级修正
    bold: bool = False          # 强调字体 → `**` 粗体(实际作用于 pymupdf 后端)
    images: bool = True         # 图片引用存在性校验

    @classmethod
    def from_config(cls, config: dict | None = None) -> "CleanOptions":
        cfg = (config or {}).get("clean") or {}
        opts = cls()
        for key in CLEAN_KEYS:
            if cfg.get(key) is not None:
                setattr(opts, key, bool(cfg[key]))
        return opts

    def disable(self, keys: Iterable[str] | str | None) -> None:
        for key in normalize_clean_keys(keys):
            setattr(self, key, False)

    def enabled(self) -> tuple[str, ...]:
        """本次开启的开关名(按 ``CLEAN_KEYS`` 顺序)。"""
        return tuple(k for k in CLEAN_KEYS if getattr(self, k))

    def to_dict(self) -> dict:
        return {key: bool(getattr(self, key)) for key in CLEAN_KEYS}

    def apply(self, config: dict) -> None:
        """把开关落到各模块实际读取的位置。

        bold 只在 pymupdf 后端生效(需要 PDF 的字体信息),关闭等价于
        `pymupdf.bold_fonts` 为空 —— 后端据此跳过字体标注。
        """
        if not self.bold:
            config.setdefault("pymupdf", {})["bold_fonts"] = []


def normalize_clean_keys(keys: Iterable[str] | str | None) -> list[str]:
    """规范化清理项名(接受 `-`/`_` 混用、逗号或空格分隔),未知项报错。"""
    if keys is None:
        return []
    if isinstance(keys, str):
        keys = re.split(r"[,\s]+", keys)
    out: list[str] = []
    for raw in keys:
        key = str(raw).strip().lower().replace("-", "_")
        if not key:
            continue
        if key not in CLEAN_KEYS:
            raise ValueError(f"未知清理项: {raw!r}(可选: {', '.join(CLEAN_KEYS)})")
        if key not in out:
            out.append(key)
    return out


def resolve_options(config: dict | None = None,
                    disable: Iterable[str] | str | None = None) -> CleanOptions:
    """配置默认值 + 本次运行的关闭项 → 最终 CleanOptions。"""
    opts = CleanOptions.from_config(config)
    opts.disable(disable)
    return opts
