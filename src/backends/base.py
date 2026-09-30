"""OCR/PDF 后端统一接口(idea.md §13)。

所有后端(本地 PyMuPDF4LLM、云端 MinerU、云端 PaddleOCR-VL)
必须实现统一的 convert() 接口,输出统一的 work/book.md + images/ 结构,
使后续 Markdown 清理 → Pandoc → EPUB 流程与具体后端解耦。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConversionResult:
    """统一转换结果。"""

    book_md: Path          # work/book.md(Markdown 引用 images/ 相对路径)
    images_dir: Path       # work/images/
    backend: str           # 后端名: pymupdf / mineru / paddleocr
    task_id: str | None = None   # 云端任务 id(本地后端为 None)
    stats: dict = field(default_factory=dict)  # 统计信息(图片数、字符数等)


class Backend(ABC):
    """PDF → 统一 Markdown 后端接口。"""

    #: 后端标识名(用于配置切换)
    name: str = "base"

    @abstractmethod
    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        """将 PDF 转换为 work_dir/book.md + work_dir/images/。"""


# ---------- 云端任务续跑 ----------

TASK_CACHE_FILE = ".ocr_task.json"


def file_fingerprint(path: str | Path) -> str:
    """文件内容指纹(blake2b 8 字节)。

    用内容而非 mtime:文件复制/重新渲染后仍能命中已提交的云任务。
    """
    h = hashlib.blake2b(digest_size=8)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TaskCache:
    """work/<书名>/ 内的云端任务缓存。

    云端 OCR 提交后把 batch/job id 落盘:进程被中断(超时、Ctrl+C、关闭桌面端)
    后重跑时可直接继续轮询同一个任务,**不重新上传、不重复扣配额**。

    命中条件:同后端 + 文件内容指纹一致 + 变体一致(原文件 / 渲染纯图)。
    提交成功后写入,结果解包成功后清除。
    """

    def __init__(self, work_dir: str | Path, backend: str) -> None:
        self.path = Path(work_dir) / TASK_CACHE_FILE
        self.backend = backend

    def load(self, source: str | Path, variant: str = "original") -> dict | None:
        """返回可复用的任务信息(不匹配则 None)。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            data.get("backend") == self.backend
            and data.get("variant") == variant
            and data.get("fingerprint") == file_fingerprint(source)
        ):
            return data
        return None

    def save(self, source: str | Path, variant: str, **payload) -> None:
        data = {
            "backend": self.backend,
            "variant": variant,
            "fingerprint": file_fingerprint(source),
            "source": str(source),
            "created_at": int(time.time()),
            **payload,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------- 失败分类:哪些错误重试是有意义的 ----------


class BackendError(RuntimeError):
    """后端错误基类。

    ``retryable`` 是这套错误最要紧的信息:批处理按它决定「再跑一次」还是「当场判死」。
    重试一个认证失败/参数错误/配额用尽的书只是白跑一轮,扫描书还会重复消耗云端额度,
    所以这类错误必须显式标成不可重试;网络抖动、轮询超时才是可重试的。
    """

    retryable = True

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


def is_retryable(error: BaseException) -> bool:
    """该异常是否值得重试(默认 True:未知异常宁可给一次机会,但已知的确定性失败要自己标)。"""
    return bool(getattr(error, "retryable", True))


# ---------- 云端结果缓存(success 之后重跑不再重复计费) ----------

#: 段结果落盘目录(与 .ocr_task.json 同级,都在工作目录内)
PARTS_DIR = "_parts"
PART_MD_FILE = "full.md"
PART_IMAGES_DIR = "images"
PART_META_FILE = ".part.json"
PART_SCHEMA = 1


def params_fingerprint(params: dict) -> str:
    """云端 OCR 参数指纹:换模型/语言/开关后不能复用旧结果(键序无关)。"""
    payload = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=6).hexdigest()


@dataclass(frozen=True)
class OcrPart:
    """一段云端 OCR(一个提交单元)。

    ``part_id`` 必须**跨批次稳定**(MinerU 用页码范围 ``"1-200"``,整本单任务用
    ``"1-<总页数>"``,PaddleOCR 用 ``"whole"``):段结果的落盘缓存、重跑时只补缺失段、
    按段序合并,都只认它,不认批次内的序号。``page_range`` 是要提交给云端的
    page_ranges(整本时为 None,不带该字段)。
    """

    idx: int
    part_id: str
    page_range: str | None = None


def _safe_part_name(part_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", part_id) or "part"


class PartStore:
    """云端 OCR 段结果的落盘缓存(``work_dir/_parts/<part_id>/``)。

    **为什么需要**:云端任务一旦成功就已经计费,但结果此前只活在内存里 —— 之后任何一次
    重跑(清理/EPUB 阶段失败后的重试、hybrid 后续区段中断、用户手动重跑)都会重新上传、
    重复 OCR、重复扣额度。只缓存「已提交的云端任务」拦不住这种重跑:任务已取回、
    `.ocr_task.json` 已清除,于是整本书被从头再 OCR 一遍。

    结构::

        work/<书>/_parts/1-200/full.md      云端原样 Markdown(目录名即段标识)
        work/<书>/_parts/1-200/images/      云端原样图片
        work/<书>/_parts/1-200/.part.json   校验信息

    ``.part.json`` **最后写**:它是「这一段可用」的唯一标记,半份结果不会被当成命中。

    命中判定:同后端 + 源文件内容指纹一致 + OCR 参数指纹一致 + 段标识一致。
    ``resume=False``(CLI ``--no-resume``)时不读缓存但仍写 —— 强制重新提交的结果同样
    值得留给下一次重跑,不必把后续续跑能力一起废掉。
    """

    def __init__(self, work_dir: str | Path, backend: str,
                 source_fingerprint: str, *, resume: bool = True) -> None:
        self.root = Path(work_dir) / PARTS_DIR
        self.backend = backend
        self.source_fingerprint = source_fingerprint
        self.resume = resume

    def dir_for(self, part_id: str) -> Path:
        return self.root / _safe_part_name(part_id)

    def md_path(self, part_id: str) -> Path:
        return self.dir_for(part_id) / PART_MD_FILE

    def _key(self, part_id: str, params_fp: str) -> dict:
        return {
            "schema": PART_SCHEMA,
            "backend": self.backend,
            "part_id": part_id,
            "fingerprint": self.source_fingerprint,
            "params": params_fp,
        }

    def has_result(self, part_id: str, params_fp: str) -> bool:
        """磁盘上是否已有这一段的**有效**结果(事实判断,不受 resume 开关影响)。

        与 load() 的分工:has_result 回答「这段的结果在不在、对不对」,load 回答
        「这次能不能直接拿它当结果」。——no-resume 时我们仍然要能看见**本次刚写下去**
        的结果,否则一边下载一边把自己判成"还缺"。
        """
        meta = _read_json(self.dir_for(part_id) / PART_META_FILE)
        if not meta:
            return False
        want = self._key(part_id, params_fp)
        if any(meta.get(k) != v for k, v in want.items()):
            return False
        return self.md_path(part_id).is_file()

    def load(self, part_id: str, params_fp: str) -> Path | None:
        """能复用的段结果目录,否则 None(未落盘 / 指纹或参数不符 / 已禁用 resume)。"""
        if not self.resume:
            return None
        if not self.has_result(part_id, params_fp):
            return None
        return self.dir_for(part_id)

    def save(self, part_id: str, params_fp: str, md_text: str,
             images_src: str | Path | None = None, **extra) -> Path:
        """落盘一段结果(先写内容,最后写 meta)。"""
        part_dir = self.dir_for(part_id)
        part_dir.mkdir(parents=True, exist_ok=True)
        self.md_path(part_id).write_text(md_text, encoding="utf-8")
        if images_src is not None:
            src = Path(images_src)
            if src.is_dir():
                dst = part_dir / PART_IMAGES_DIR
                dst.mkdir(parents=True, exist_ok=True)
                for img in sorted(src.iterdir()):
                    if img.is_file():
                        shutil.copy2(img, dst / img.name)
        meta = {**self._key(part_id, params_fp), "created_at": int(time.time()), **extra}
        (part_dir / PART_META_FILE).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return part_dir

    def images_dir(self, part_id: str) -> Path:
        return self.dir_for(part_id) / PART_IMAGES_DIR


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def normalize_image_refs(md: str, images_abs: Path, images_dir: str = "images") -> str:
    """把 Markdown 中的图片引用前缀规范化为相对 images/。

    不同后端输出的引用前缀不同(绝对路径 / 相对路径 / 相对 cwd / 正反斜杠),
    统一替换为 "images/xxx"(相对 work_dir)。
    """
    import os

    prefixes = {
        str(images_abs),
        str(images_abs).replace("\\", "/"),
        str(images_abs.resolve()),
        str(images_abs.resolve()).replace("\\", "/"),
    }
    # 相对 cwd 变体(pymupdf4llm 会把 image_path 相对化为 cwd 形式)
    try:
        rel_cwd = os.path.relpath(images_abs, os.getcwd())
        prefixes.add(rel_cwd)
        prefixes.add(rel_cwd.replace("\\", "/"))
    except ValueError:
        pass
    for prefix in prefixes:
        md = md.replace(f"]({prefix}/", f"]({images_dir}/")
        md = md.replace(f'"]("{prefix}/', f'"]("{images_dir}/')  # 防御:带引号形式
    return md
