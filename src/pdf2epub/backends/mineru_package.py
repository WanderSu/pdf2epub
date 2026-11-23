"""MinerU 结果包:白名单解压、结构化结果定位、图片并入与页码注释。

结果包来自云端,解压走 :func:`pdf2epub.backends.safe_zip.safe_extract`(拒绝路径
穿越/符号链接/超限)。这里只回答三件事:包里哪些条目有用、结构化结果在哪、
某一段的图片怎么并进统一的 ``images/`` 而不互相覆盖。
"""
from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from pdf2epub.page_result import page_marker_from_range

from .safe_zip import safe_extract

#: 段缓存里附带的 MinerU 结构化结果文件名(`full.md` 不含页脚脚注,脚注只能靠它补)
CONTENT_LIST_FILE = "content_list.json"

#: 结果包里真正要落盘的条目后缀:`*_content_list.json` / `*_content_list_v2.json`
WANTED_SUFFIXES = ("_content_list.json", "_content_list_v2.json")


def wanted_member(name: str) -> bool:
    """结果包里要不要这个条目(其余 layout.json、原始 PDF 等一律不解压)。"""
    base = name.rsplit("/", 1)[-1]
    if base == "full.md":
        return True
    if name.startswith("images/") or "/images/" in name:
        return True
    return base.endswith(WANTED_SUFFIXES)


def extract_package(zip_path: str | Path, dest: str | Path) -> list[Path]:
    """按白名单把结果包解到 ``dest``,返回写出的文件。"""
    return safe_extract(zip_path, dest, wanted=wanted_member)


def content_list_files(extract_dir: Path) -> dict[str, Path]:
    """结果包里要随段一起缓存的结构化结果(v1 ``*_content_list.json`` 优先,退回 v2)。"""
    for pattern in ("*_content_list.json", "*_content_list_v2.json"):
        candidates = sorted(extract_dir.rglob(pattern))
        if candidates:
            return {CONTENT_LIST_FILE: candidates[0]}
    return {}


def copy_part_images(src_dir: Path, dst_dir: Path, md: str, *, prefix: str) -> tuple[str, int]:
    """把某段的图片并入统一 ``images/``,并保持「同一输入 → 同一文件名」。

    三种情形:
      - 目标不存在 → 直接复制,引用不变
      - 目标已存在且内容相同 → 什么都不做(重跑复用同一张图,产物稳定、不产生垃圾文件)
      - 目标已存在但内容不同(跨段重名) → 加段前缀并同步改引用
    """
    if not src_dir.is_dir():
        return md, 0
    count = 0
    for img in sorted(src_dir.iterdir()):
        if not img.is_file():
            continue
        target = dst_dir / img.name
        if target.exists():
            if filecmp.cmp(img, target, shallow=False):
                count += 1
                continue
            new_name = f"{prefix}{img.name}"
            shutil.copy2(img, dst_dir / new_name)
            md = md.replace(f"images/{img.name}", f"images/{new_name}")
        else:
            shutil.copy2(img, target)
        count += 1
    return md, count


def part_marker(page_range: str | None, idx: int) -> str:
    """分片段落的页码注释:优先用提交时的真实 page_range,缺失时退化为段序标记。"""
    if page_range:
        return page_marker_from_range(page_range)
    return f"<!-- page-group {idx} -->"
