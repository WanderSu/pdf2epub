"""云端结果包的安全解压(不信任 full_zip_url 指向的 ZIP)。

威胁模型:压缩包由第三方服务生成,可能被劫持、被替换或本身带恶意条目。``extractall``
会照着条目名往磁盘上写,遇到绝对路径、``..`` 穿越或符号链接就会写到目标目录之外;
超大条目还能当解压炸弹把磁盘塞满。因此这里不调 ``extractall``,而是:

  - 只读白名单内的条目(``full.md`` / ``images/`` / ``*_content_list*.json``),
    其余一概不落盘;
  - 逐条校验名字:反斜杠归一、拒绝绝对路径与盘符、拒绝任何 ``..`` 段、拒绝 NUL;
  - 拒绝符号链接与非普通文件条目;
  - 限制条目数、单文件大小与总解压大小;
  - 写盘前按 ``resolve()`` 再确认一次「最终路径仍在目标目录内」,并核对落盘字节数。

任何一条不满足都抛 :class:`UnsafeArchiveError` —— 宁可整段判失败(可重试),也不写
出目标目录。
"""
from __future__ import annotations

import shutil
import stat
import zipfile
from pathlib import Path

#: 条目总数上限(MinerU 结果包实测几十到几百条,五千足够宽松)
DEFAULT_MAX_ENTRIES = 5000
#: 单个条目解压后的大小上限(单页大图也不会到 64 MB)
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
#: 整包解压后的大小上限
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024

_S_IFMT = 0o170000
_S_IFLNK = 0o120000


class UnsafeArchiveError(ValueError):
    """压缩包内容不可信:路径穿越、绝对路径、符号链接或超过体积/数量上限。"""


def _check_name(name: str) -> str:
    """校验条目名并返回归一化后的相对路径(不合法直接抛)。"""
    if "\x00" in name:
        raise UnsafeArchiveError(f"条目名含 NUL 字符: {name!r}")
    raw = name.replace("\\", "/")
    if raw.startswith("/"):
        raise UnsafeArchiveError(f"条目使用绝对路径: {name!r}")
    if len(raw) > 1 and raw[1] == ":":
        raise UnsafeArchiveError(f"条目使用盘符路径: {name!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise UnsafeArchiveError(f"条目名为空: {name!r}")
    if any(p == ".." for p in parts):
        raise UnsafeArchiveError(f"条目含上级目录引用(..): {name!r}")
    return "/".join(parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return (mode & _S_IFMT) == _S_IFLNK


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    return True


def safe_extract(
    zip_path: str | Path,
    dest: str | Path,
    *,
    wanted=None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> list[Path]:
    """把 ``zip_path`` 中通过 ``wanted`` 过滤的安全条目解压到 ``dest``。

    wanted: ``(相对路径) -> bool``;为 None 时解压全部(仍逐条做安全校验)。
    返回实际写出的文件路径列表。
    """
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    root = dest_path.resolve()

    written: list[Path] = []
    total = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise UnsafeArchiveError(
                f"压缩包条目数 {len(infos)} 超过上限 {max_entries}"
            )
        for info in infos:
            rel = _check_name(info.filename)
            if info.is_dir():
                continue
            if _is_symlink(info):
                raise UnsafeArchiveError(f"拒绝符号链接条目: {info.filename!r}")
            if wanted is not None and not wanted(rel):
                continue
            if info.file_size > max_file_bytes:
                raise UnsafeArchiveError(
                    f"条目 {rel!r} 解压后 {info.file_size} 字节,超过单文件上限 {max_file_bytes}"
                )
            total += info.file_size
            if total > max_total_bytes:
                raise UnsafeArchiveError(
                    f"解压总量超过上限 {max_total_bytes} 字节(条目 {rel!r} 处)"
                )

            target = dest_path.joinpath(*rel.split("/"))
            if not _inside(root, target):
                raise UnsafeArchiveError(f"条目会写到目标目录之外: {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # mkdir 之后再确认一次:目标目录里若有预置的符号链接父目录,resolve()
            # 会指到目录之外
            if not _inside(root, target.parent):
                raise UnsafeArchiveError(f"父目录指向目标目录之外: {info.filename!r}")

            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            if target.stat().st_size != info.file_size:
                target.unlink(missing_ok=True)
                raise UnsafeArchiveError(f"条目 {rel!r} 落盘大小与声明不符(可能被截断)")
            written.append(target)
    return written
