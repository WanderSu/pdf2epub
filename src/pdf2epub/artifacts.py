"""产物路径、撞名判定与失败产物收尾(从 batch.py 拆出)。

这里只放"针对磁盘上产物的纯逻辑":输出名怎么取、两个源文件会不会写进同一个位置、
失败产物怎么移出完成判定的视野、上一轮成品怎么保全与还原。转换流程本身(任务状态、
重试、汇总)仍留在 batch.py —— 拆开的目的是让这些规则能被单独测试与复用,
而不是把批处理拆成一层层转发。
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from pdf2epub.epub.verify import archive_issue

#: 保持既有日志通道名,拆模块不改日志归属
logger = logging.getLogger("pdf2epub.batch")

#: 失败产物的改名后缀:失败产物要移出"完成判定"的视野,但保留现场供排查
FAILED_OUTPUT_SUFFIX = ".failed"


def sanitize_name(name: str) -> str:
    """规范化**工作目录**名:空白与非法字符 → 下划线。
    必要原因:PyMuPDF 的 C 库保存图片时会把路径中的空格替换为下划线,
    含空格的工作目录会导致图片写入失败。
    """
    name = re.sub(r"[\s]+", "_", name.strip())
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    return name or "book"


def output_stem(name: str) -> str:
    """输出 EPUB 的**文件名**(保留原文空格)。

    与工作目录不同,EPUB 由 Pandoc 写出、不经过 PyMuPDF,无需把空格换成下划线:
    保留「标题 - 作者」原貌更易读,也让书库的「 - 」分隔解析与 EPUB 元数据一致。
    仅替换 Windows 非法字符并去掉结尾空白/点。
    """
    name = re.sub(r'[\\/:*?"<>|]+', "_", name.strip()).rstrip(". ").strip()
    return name or "book"


def epub_path(source: Path, output_dir: Path) -> Path:
    """输出 EPUB 路径(保留原文空格,与 output_stem 一致)。"""
    return output_dir / f"{output_stem(source.stem)}.epub"


def existing_epub(source: Path, output_dir: Path) -> Path | None:
    """已存在的输出 EPUB:优先新命名,兼容旧版本的下划线命名。

    旧版(≤v0.2.1)输出的文件名把空格 sanitize 成下划线;升级后若只认新名,
    已完成的输出会被当成未完成而重复转换,所以跳过判断要认两种命名。
    """
    for p in (epub_path(source, output_dir),
              output_dir / f"{sanitize_name(source.stem)}.epub"):
        if p.exists():
            return p
    return None


def source_identity(path: Path) -> str:
    """源文件身份键(Windows 路径大小写不敏感,比较前统一规范化)。"""
    return os.path.normcase(str(Path(path).resolve()))


def name_conflicts(sources: list[Path]) -> dict[str, list[Path]]:
    """同一批输入里**会写进同一个 work/ 与 output/ 路径**的源文件。

    两个不同的源文件撞名(工作目录名或输出 EPUB 名相同)时,先转的那个会占住
    `work/<名>/` 与 `output/<名>.epub`,后一个随即被 `is_done()` 判成「已完成」而
    静默跳过 —— 用户以为两本都转了,交付物却少一本(工作目录还会串用上一本的图片)。
    撞名时不猜谁该赢、也不覆盖:显式列出冲突,由 process_batch 判失败。

    返回 ``{源文件身份键: [与之撞名的其它源文件]}``;同一条路径重复传入不算冲突。

    键要按大小写归一(``casefold``):Windows/macOS 默认文件系统大小写不敏感,
    ``Book.pdf`` 与 ``book.pdf`` 会写进同一个 ``work/Book/`` 与 ``output/Book.epub``;
    不归一的话这种「只差大小写」的撞名漏检,后一本照旧被 ``is_done`` 静默跳过。
    """
    def work_key(p: Path) -> str:
        return f"work:{sanitize_name(p.stem).casefold()}"

    def out_key(p: Path) -> str:
        return f"out:{output_stem(p.stem).casefold()}"

    conflicts: dict[str, list[Path]] = {}
    for key_of in (work_key, out_key):          # 工作目录名与输出名各自判重
        groups: dict[str, list[Path]] = {}
        for src in sources:
            groups.setdefault(key_of(src), []).append(src)
        for group in groups.values():
            if len({source_identity(p) for p in group}) < 2:
                continue
            for src in group:
                me = source_identity(src)
                others = conflicts.setdefault(me, [])
                others.extend(p for p in group
                              if source_identity(p) != me and p not in others)
    return conflicts


def stat_signature(path: Path) -> tuple[int, float] | None:
    """(大小, mtime) 快照;文件不存在时返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime)


def discard_failed_output(epub: Path, before: tuple[int, float] | None) -> Path | None:
    """把**本次运行写出的失败产物**移出 output/,免得下次被 is_done() 判成「已完成」。

    失败结果不能伪装成成功结果:`--strict` 判失败时(pandoc 只写了一半时同理)产物
    已经在 output/ 里 —— 非空、mtime 比源新,下一次运行会直接跳过它,用户既看不到
    失败,也没法重转。

    只有在文件确实是**本次运行**写出的(大小/mtime 与运行前不同)时才动它:`--force`
    重转时若失败发生在构建之前,output 里放的是上一轮**成功**的成果,不能动它。

    改名不删除(现场留着排查);改名失败时退而删除 —— 总之不能把它留在那个位置上。
    """
    after = stat_signature(epub)
    if after is None or after == before:
        return None
    target = epub.with_name(epub.name + FAILED_OUTPUT_SUFFIX)
    try:
        os.replace(epub, target)
    except OSError as e:
        logger.warning("失败产物改名失败(%s),改为删除以免被当成已完成", e)
        try:
            epub.unlink()
        except OSError as e2:
            logger.error("失败产物既移不走也删不掉(%s): %s 可能被下次运行判成已完成",
                         e2, epub)
        return None
    logger.warning("失败产物已移到 %s(不会被当成已完成)", target.name)
    return target


def preserve_previous_output(epub: Path) -> Path | None:
    """在**覆盖之前**把上一轮的有效成品另存一份;无需保全时返回 None。

    为什么仍然需要:构建已经改成「先写 ``.part`` → 校验 → 原子 replace」
    (见 ``epub.pandoc.build_epub``),所以构建过程被打断不会再毁掉旧书;但
    **replace 之后的校验**仍可能判失败(断链/内容缩水),此时 output/ 里已经是新
    产物,而失败产物又要改名移出 output/(见 :func:`discard_failed_output`)——
    没有备份的话,用户重转一次,原本能读的那本书就没了。「重转失败」正确的结果是
    回到「还有上一本」,而不是连上一本一起赔进去。

    只保全**上一轮产物本身是完整 EPUB 包**的情况:残缺产物(``archive_issue``
    非空)本来就该被重转覆盖,不值得留,留着还会被 ``is_done()`` 判成已完成。
    """
    if not epub.is_file():
        return None
    if archive_issue(epub) is not None:
        return None
    try:
        handle, tmp = tempfile.mkstemp(prefix=f"{epub.stem}.", suffix=".epub.previous")
        os.close(handle)
    except OSError as e:
        logger.warning("上一轮成品无法备份(本次失败后将不做保全): %s", e)
        return None
    try:
        shutil.copy2(epub, tmp)
    except OSError as e:
        logger.warning("上一轮成品无法备份(本次失败后将不做保全): %s", e)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return None
    return Path(tmp)


def restore_previous_output(epub: Path, backup: Path | None) -> bool:
    """把备份的上一轮成品放回原位;确实放回了才返回 True。

    原位那份**本来还是好的**就不动它 —— 失败发生在构建之前时正是这种情况
    (``discard_failed_output`` 也不会动它)。还原后它是完整 EPUB,``is_done()``
    照旧按「完整 + 不早于源文件」判断,不会因为被还原就蒙混过关。
    """
    if backup is None or not backup.is_file():
        return False
    if epub.is_file() and archive_issue(epub) is None:
        return False
    try:
        shutil.copy2(backup, epub)
    except OSError as e:
        logger.error("上一轮成品恢复失败(%s): %s 可能需要重新转换", e, epub)
        return False
    logger.warning("已恢复上一轮的成品 %s(本次产物未通过校验,改名保留在 .failed)",
                   epub.name)
    return True


def drop_previous_backup(backup: Path | None) -> None:
    """任务结束后清掉备份(它只在失败的那一瞬间有用)。"""
    if backup is None:
        return
    with contextlib.suppress(OSError):
        backup.unlink()


