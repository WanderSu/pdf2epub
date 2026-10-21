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


#: 渲染纯图 PDF 的输入变体标识前缀(见 PartStore 的变体隔离)
VARIANT_ORIGINAL = "original"


def render_fingerprint(dpi: int = 150, jpg_quality: int = 85) -> str:
    """渲染参数指纹 —— 「同一本书的哪种渲图方式」的稳定名字。

    为什么不用渲染产物的内容指纹当键:缓存命中检查发生在**渲染之前**,为了算指纹
    专门渲染一遍,正好把"省时间"的目的抵消掉。参数能先算,且足以区分 150/300dpi。
    """
    try:
        import fitz
        mupdf = str(getattr(fitz, "VersionBind", "") or "")
    except Exception:                                    # pragma: no cover - 缺 pymupdf
        mupdf = "unknown"
    return params_fingerprint({
        "render_schema": 1,
        "dpi": int(dpi),
        "quality": int(jpg_quality),
        "mupdf": mupdf,
    })[:6]


def rendered_variant(dpi: int = 150, jpg_quality: int = 85) -> str:
    """渲染纯图变体的完整 token:``rendered:<render_fp>``。"""
    return f"rendered:{render_fingerprint(dpi, jpg_quality)}"


class TaskCache:
    """work/<书名>/ 内的云端任务缓存。

    云端 OCR 提交后把 batch/job id 落盘:进程被中断(超时、Ctrl+C、关闭桌面端)
    后重跑时可直接继续轮询同一个任务,**不重新上传、不重复扣配额**。

    命中条件:同后端 + 文件内容指纹一致 + 变体一致(原文件 / 渲染纯图)。
    提交成功后写入,结果解包成功后清除。

    **变体隔离**:``original`` 仍写 ``.ocr_task.json``(路径不变,老缓存继续有效);
    其他变体写 ``.ocr_task.<变体>.json``。否则「原文件那批任务还没取回」与
    「渲染纯图那批任务还没取回」会互相覆盖 —— 变体不同就是两次不同的上传。
    """

    def __init__(self, work_dir: str | Path, backend: str, variant: str = VARIANT_ORIGINAL) -> None:
        self.dir = Path(work_dir)
        self.variant = variant or VARIANT_ORIGINAL
        self.backend = backend
        self.path = self.dir / self._file_name(self.variant)

    @staticmethod
    def _file_name(variant: str) -> str:
        if variant == VARIANT_ORIGINAL:
            return TASK_CACHE_FILE
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", variant)
        return f"{TASK_CACHE_FILE[:-5]}.{safe}.json"

    def load(self, source: str | Path, variant: str | None = None) -> dict | None:
        """返回可复用的任务信息(不匹配则 None)。"""
        want = variant or self.variant
        if want != self.variant:
            # 调用方查的是另一个变体的缓存:那不是这份文件的职责(各变体各一个文件)
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            data.get("backend") == self.backend
            and data.get("variant") == want
            and data.get("fingerprint") == file_fingerprint(source)
        ):
            return data
        return None

    def save(self, source: str | Path, variant: str, **payload) -> None:
        if variant != self.variant:
            raise ValueError(f"TaskCache(variant={self.variant}) 不能写 {variant} 的任务")
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
    """云端 OCR 段结果的落盘缓存(``work_dir/_parts/<part_id>[_<变体>]/``)。

    **为什么需要**:云端任务一旦成功就已经计费,但结果此前只活在内存里 —— 之后任何一次
    重跑(清理/EPUB 阶段失败后的重试、hybrid 后续区段中断、用户手动重跑)都会重新上传、
    重复 OCR、重复扣额度。只缓存「已提交的云端任务」拦不住这种重跑:任务已取回、
    `.ocr_task.json` 已清除,于是整本书被从头再 OCR 一遍。

    结构::

        work/<书>/_parts/1-200/full.md      云端原样 Markdown(目录名即段标识)
        work/<书>/_parts/1-200/images/      云端原样图片
        work/<书>/_parts/1-200/.part.json   校验信息

    ``.part.json`` **最后写**:它是「这一段可用」的唯一标记,半份结果不会被当成命中。

    命中判定(段身份,阶段 B §E.8):同后端 + 源文件内容指纹一致 + OCR 参数指纹一致 +
    段标识一致 + **输入变体一致**。

    **输入变体为什么进键**:同一个段 id、同一份源文件指纹,「提交原文件」与「提交渲染
    纯图 PDF」是两次不同的 OCR —— 云端看到的是两张不同的图。此前 ``variant`` 只写在
    ``.part.json`` 里当附注、匹配时不看,于是渲染段能命中原始段、两者还会写同一个目录
    (互相覆盖)。现在:

        original            → ``_parts/<part_id>/``(路径不变,老缓存继续命中)
        rendered:<render_fp> → ``_parts/<part_id>__rendered_<render_fp>/``

    老 ``.part.json`` 没有 ``variant`` 字段,读的时候按 ``original`` 归一 —— 那是当时
    唯一存在的变体,不这么兼容会让现存缓存整批失效、重跑重扣额度。

    ``resume=False``(CLI ``--no-resume``)时不读缓存但仍写 —— 强制重新提交的结果同样
    值得留给下一次重跑,不必把后续续跑能力一起废掉。
    """

    def __init__(self, work_dir: str | Path, backend: str,
                 source_fingerprint: str, *, resume: bool = True,
                 variant: str = VARIANT_ORIGINAL) -> None:
        self.root = Path(work_dir) / PARTS_DIR
        self.backend = backend
        self.source_fingerprint = source_fingerprint
        self.resume = resume
        self.variant = variant or VARIANT_ORIGINAL

    def dir_for(self, part_id: str) -> Path:
        name = _safe_part_name(part_id)
        if self.variant != VARIANT_ORIGINAL:
            name = f"{name}__{_safe_part_name(self.variant)}"
        return self.root / name

    def md_path(self, part_id: str) -> Path:
        return self.dir_for(part_id) / PART_MD_FILE

    def _key(self, part_id: str, params_fp: str) -> dict:
        return {
            "schema": PART_SCHEMA,
            "backend": self.backend,
            "part_id": part_id,
            "fingerprint": self.source_fingerprint,
            "params": params_fp,
            "variant": self.variant,
        }

    def _matches(self, meta: dict, want: dict) -> bool:
        """逐字段比对;``variant`` 缺省按 ``original`` 解释(兼容旧缓存)。"""
        for key, value in want.items():
            have = meta.get(key)
            if key == "variant" and "variant" not in meta:
                have = VARIANT_ORIGINAL
            if have != value:
                return False
        return True

    def has_result(self, part_id: str, params_fp: str) -> bool:
        """磁盘上是否已有这一段的**有效**结果(事实判断,不受 resume 开关影响)。

        与 load() 的分工:has_result 回答「这段的结果在不在、对不对」,load 回答
        「这次能不能直接拿它当结果」。——no-resume 时我们仍然要能看见**本次刚写下去**
        的结果,否则一边下载一边把自己判成"还缺"。
        """
        meta = _read_json(self.dir_for(part_id) / PART_META_FILE)
        if not meta:
            return False
        if not self._matches(meta, self._key(part_id, params_fp)):
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
             images_src: str | Path | None = None, *,
             extra_files: dict[str, str | Path] | None = None, **extra) -> Path:
        """落盘一段结果(先写内容与附带文件,最后写 meta)。

        extra_files:随段一起缓存的**结构化结果**(如 MinerU 的 content_list.json)。
        它们必须在 meta 之前写好 —— meta 只是「这一段可用」的标记,写它的时候
        磁盘上的内容必须已经齐全。

        ``variant`` 由 store 自己写进 meta(不接受调用方覆盖):段身份必须和它所在的
        目录一致,否则又会出现"目录是渲染变体、键说是原文件"的错位。
        """
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
        for name, extra_src in (extra_files or {}).items():
            extra_path = Path(extra_src)
            if extra_path.is_file():
                shutil.copy2(extra_path, part_dir / Path(name).name)
        meta = {**self._key(part_id, params_fp), "created_at": int(time.time()), **extra,
                "variant": self.variant}
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


# ---------- 证据层(content_list):IR 清理与页账的唯一结构依据 ----------

#: 段缓存里附带的结构化结果(与 mineru/paddle 适配器写的同名)
EVIDENCE_FILE = "content_list.json"


@dataclass(frozen=True)
class EvidenceBundle:
    """一次转换能拿到的证据层(``content_list``)及其覆盖范围。

    ``items`` 是**已经按整本页码对齐**的条目(多段时按段偏移平移过 ``page_idx``)。
    ``available`` 为 False 时调用方必须如实记"无证据层":页账记 ``undecided``,
    跨块拼接的删除降级为建议 —— 不猜、不补页号。
    """

    items: tuple[dict, ...] = ()
    total_pages: int = 0
    parts: tuple[str, ...] = ()
    note: str = ""

    @property
    def available(self) -> bool:
        return bool(self.items)


def _page_idx_of(item: object) -> int | None:
    if isinstance(item, dict):
        page = item.get("page_idx")
        if isinstance(page, int) and page >= 0:
            return page
    return None


def _part_span(part_dir: Path, base_offset: int) -> tuple[int, int] | None:
    """段目录 → (整本 0-based 起页, 段内最大相对页);认不出范围就 None。

    名字形如 ``1-72``(段页码范围,后端自己给的稳定 part_id)、``whole``(整本一次提交)。
    ``__rendered_xxx`` 之类的变体后缀不影响段身份(已含在目录名里)。
    """
    name = part_dir.name.split("__")[0]
    if name == "whole":
        return base_offset, -1                      # 整本:范围待读 content_list 才知道
    m = re.match(r"(\d+)-(\d+)$", name)
    if not m:
        return None
    return base_offset + int(m.group(1)) - 1, int(m.group(2)) - int(m.group(1))


def collect_evidence(work_dir: str | Path, backend: str = "") -> EvidenceBundle:
    """从段缓存里收集证据层,并把多段拼成**整本页码**。

    为什么要这么绕:``content_list`` 是后端按段给的,``page_idx`` 是**段内**页码。
    直接拼起来会把第 2 段的第 1 页当成整本第 1 页 —— 页账全错,页归属全错。
    所以偏移只能从**段的身份**推:段目录名就是 part_id(``1-72`` / ``whole``),
    混合流程的扫描段外面还套一层 ``_scan_p<首页>``。

    拿不准则**不返回证据**(而不是返回错位的证据):页账记 ``undecided`` 是可接受的
    "说不出来",错位的页号是不可接受的"说错了"。重叠段同理 —— 说明同一批页被跑了
    两次(换了后端/换了参数),保留先出现的那段并在 note 里说明。
    """
    root = Path(work_dir)
    wanted = backend
    m = re.match(r"hybrid\((.+)\)$", backend or "")
    if m:
        wanted = m.group(1)

    spans: list[tuple[int, Path, str]] = []

    def consider(part_dir: Path, base_offset: int, label: str) -> None:
        meta = _read_json(part_dir / PART_META_FILE)
        got = str(meta.get("backend") or "")
        if wanted and got and got != wanted:
            return
        if not (part_dir / EVIDENCE_FILE).is_file():
            return
        span = _part_span(part_dir, base_offset)
        if span is None:
            return
        spans.append((span[0], part_dir, label))

    parts_root = root / PARTS_DIR
    if parts_root.is_dir():
        for child in sorted(parts_root.iterdir()):
            if child.is_dir():
                consider(child, 0, child.name)
    for scan in sorted(root.glob("_scan_p*")):
        nm = re.match(r"_scan_p(\d+)$", scan.name)
        if not nm or not scan.is_dir():
            continue
        proot = scan / PARTS_DIR
        if not proot.is_dir():
            continue
        for child in sorted(proot.iterdir()):
            if child.is_dir():
                consider(child, int(nm.group(1)) - 1, f"{scan.name}/{child.name}")

    if not spans:
        return EvidenceBundle(note="没有段缓存或段里没有 content_list.json")

    items: list[dict] = []
    used: list[str] = []
    notes: list[str] = []
    taken: list[tuple[int, int]] = []
    total = 0
    for offset, part_dir, label in sorted(spans, key=lambda t: t[0]):
        try:
            data = json.loads((part_dir / EVIDENCE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            notes.append(f"{label}: content_list 读取失败({exc})")
            continue
        if isinstance(data, dict):
            data = data.get("blocks") or data.get("content_list") or []
        if not isinstance(data, list):
            notes.append(f"{label}: content_list 形状不认识")
            continue
        local = [p for p in (_page_idx_of(it) for it in data) if p is not None]
        if not local:
            continue
        lo, hi = offset + min(local), offset + max(local)
        if any(not (hi < a or lo > b) for a, b in taken):
            notes.append(f"{label}: 页码范围 {lo}-{hi} 与已采用的段重叠,未采用")
            continue
        taken.append((lo, hi))
        total = max(total, hi + 1)
        used.append(label)
        for item in data:
            if not isinstance(item, dict):
                continue
            page = _page_idx_of(item)
            if page is None:
                items.append(dict(item))            # 没有页号的条目原样带上,不替它编页号
                continue
            moved = dict(item)
            moved["page_idx"] = page + offset
            items.append(moved)

    if not items:
        return EvidenceBundle(note="; ".join(notes) or "段里没有可用条目")
    note = "; ".join(notes)
    return EvidenceBundle(items=tuple(items), total_pages=total,
                          parts=tuple(used), note=note)
