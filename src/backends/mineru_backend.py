"""MinerU Cloud 后端(idea.md §13 / Phase 4)。

流程(官方 /api/v4 精准解析 API,本地文件上传模式):
  1. POST /api/v4/file-urls/batch  申请上传 URL + batch_id
  2. PUT  上传文件(系统自动提交解析任务)
  3. GET  /api/v4/extract-results/batch/{batch_id}  轮询至 done/failed
  4. 下载 full_zip_url → 解压(MinerU 输出:full.md + images/ + JSON)
  5. 规范化为统一 work/book.md + work/images/

>200 页自动分片(MinerU 官方单任务限制 ≤200 页 / ≤200MB):
  对同一 PDF 提交多个 files 条目,各自指定 page_ranges(如 "1-200"、"201-302"),
  同一 batch 内并行解析,结果按段序合并为统一 book.md + images/。

**续跑的两层缓存**(都不会重复上传、重复计费):
  - `.ocr_task.json`(TaskCache):已提交但**未取回结果**的批次 —— 重跑继续轮询同一个
    批次,不重新上传。
  - `_parts/<段标识>/`(PartStore):**每一段的结果一拿到就落盘**。云端任务成功即已
    计费,之后清理/EPUB 阶段失败后的重试、hybrid 后续区段中断、用户手动重跑,都只补
    缺失的段;全部命中时完全不碰云端。批处理的重试因此不再等于「从头再 OCR 一遍」。

凭证:MINERU_API_TOKEN 环境变量(不得硬编码)。
"""
from __future__ import annotations

import filecmp
import os
import shutil
import time
import zipfile
from pathlib import Path

import requests
import pymupdf

from .base import (
    PART_MD_FILE,
    Backend,
    BackendError,
    ConversionResult,
    OcrPart,
    PartStore,
    TaskCache,
    _safe_part_name,
    file_fingerprint,
    normalize_image_refs,
    params_fingerprint,
)
from page_result import page_marker_from_range
from paths import load_api_key
import events

DEFAULT_BASE_URL = "https://mineru.net/api/v4"
DEFAULT_TIMEOUT = 600      # 轮询总超时(秒)
POLL_INTERVAL = 5          # 轮询间隔(秒)
MAX_PAGES_PER_TASK = 200   # MinerU 官方单任务页数上限;超过自动按 page_ranges 分片

TERMINAL_STATES = {"done", "failed"}
STATE_LABELS = {
    "waiting-file": "等待文件上传",
    "pending": "排队中",
    "running": "解析中",
    "converting": "格式转换中",
    "done": "完成",
    "failed": "失败",
}

#: 客户端类 HTTP 状态码:认证/参数/配额问题,重试改变不了结果,只会重复消耗额度
NON_RETRYABLE_HTTP = {400, 401, 403, 404, 405, 409, 413, 422, 429}


class MinerUError(BackendError):
    """MinerU API 错误。

    err_msg 是**云端返回的原始错误信息**:降级判定只看它,不看我们自己拼的中文前缀 ——
    否则 `MinerU 解析失败: xxx` 这类消息会让任何失败都被误判成「解析失败」而触发
    渲染纯图重试(白跑一轮、重复消耗额度)。
    """

    def __init__(self, message: str, *, err_msg: str | None = None,
                 retryable: bool | None = None) -> None:
        super().__init__(message, retryable=retryable)
        self.err_msg = message if err_msg is None else err_msg


def _is_parse_failure(err_msg: str) -> bool:
    """判断是否属于「MinerU 解析不动这个文件」,只有这种才值得降级重试。"""
    return "parsing failed" in err_msg or "解析失败" in err_msg


class MinerUAdapter(Backend):
    name = "mineru"

    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        model_version: str = "vlm",
        is_ocr: bool = True,
        enable_formula: bool = True,
        enable_table: bool = True,
        language: str = "ch",
        timeout: int = DEFAULT_TIMEOUT,
        poll_interval: int = POLL_INTERVAL,
        max_pages_per_task: int = MAX_PAGES_PER_TASK,
        resume: bool = True,
    ) -> None:
        self.token = (
            token
            or os.environ.get("MINERU_API_TOKEN", "")
            or load_api_key("MinerU")
            or ""
        )
        if not self.token:
            raise MinerUError(
                "缺少 MinerU API Token:请设置环境变量 MINERU_API_TOKEN 或项目根目录 apikey.json",
                retryable=False,     # 认证缺失重试多少次都一样
            )
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.language = language
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_pages_per_task = max(max_pages_per_task, 1)
        self.resume = resume

    # ---------- HTTP 基础 ----------
    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    def _post_json(self, path: str, payload: dict) -> dict:
        resp = requests.post(f"{self.base_url}{path}", headers=self._headers, json=payload, timeout=60)
        return self._parse(resp)

    def _get_json(self, path: str) -> dict:
        resp = requests.get(f"{self.base_url}{path}", headers=self._headers, timeout=60)
        return self._parse(resp)

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        if resp.status_code != 200:
            raise MinerUError(
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code not in NON_RETRYABLE_HTTP,
            )
        data = resp.json()
        if data.get("code") != 0:
            # API 层判定的错误(参数/配额/权限):重试无意义
            raise MinerUError(
                f"API 错误 code={data.get('code')} msg={data.get('msg')}",
                retryable=False,
            )
        return data

    # ---------- 核心流程 ----------
    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        pdf_path = Path(pdf_path)
        work_dir = Path(work_dir)
        if not pdf_path.exists():
            raise MinerUError(f"文件不存在: {pdf_path}", retryable=False)

        try:
            result = self._convert_inner(pdf_path, work_dir, variant="original")
        except MinerUError as e:
            # 伪文字层等结构异常的文件 MinerU 会解析失败,降级为渲染纯图后重试;
            # 其他错误(网络、额度、格式不支持)直接抛 —— 重试只是白跑一轮
            if not _is_parse_failure(e.err_msg):
                raise
            print("[mineru] 原文件解析失败(可能为伪文字层),降级为渲染纯图后重试 ...")
            self._cache(work_dir).clear()   # 原文件那批任务已判死,缓存作废
            rendered = self._render_to_image_pdf(pdf_path, work_dir)
            try:
                # source_pdf=原文件:段缓存指纹跟着原始 PDF,降级只重做真正解析失败的
                # 那些段,已经成功的段照样复用
                result = self._convert_inner(
                    rendered, work_dir, variant="rendered", source_pdf=pdf_path
                )
                print("[mineru] 降级 OCR 成功(渲染纯图版)")
            finally:
                rendered.unlink(missing_ok=True)
        stats = result.stats
        print(f"[mineru] 解析完成: {stats.get('parts')} 段 / "
              f"{stats.get('total_pages')} 页 / {stats.get('images')} 张图")
        return result

    @staticmethod
    def _cache(work_dir: Path) -> TaskCache:
        return TaskCache(work_dir, MinerUAdapter.name)

    def _params_fp(self) -> str:
        """OCR 参数指纹:换模型/语言/开关后不能复用旧结果。"""
        return params_fingerprint({
            "model_version": self.model_version,
            "is_ocr": self.is_ocr,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        })

    def _convert_inner(
        self,
        pdf_path: Path,
        work_dir: Path,
        variant: str = "original",
        source_pdf: Path | None = None,
    ) -> ConversionResult:
        """核心转换:按段提交云端任务,段结果落盘,重跑只补缺失的段。

        variant/source_pdf:降级重试时 pdf_path 是本地渲染的纯图临时文件,而段缓存 /
        任务缓存的内容指纹要跟着**原始 PDF**(临时文件每次重渲染字节可能不同)。
        variant 只记录在段元信息里,不进缓存键 —— 段的身份是「哪本书的哪几页 + OCR
        参数」,换上传变体(原文件 / 渲染纯图)不影响已取得的那几页结果的价值。
        """
        source = source_pdf or pdf_path
        cache = self._cache(work_dir)
        params_fp = self._params_fp()
        store = PartStore(work_dir, self.name, file_fingerprint(source), resume=self.resume)

        total_pages = self._count_pages(pdf_path)
        parts = self._plan_parts(total_pages)
        sharded = len(parts) > 1
        kind = "渲染纯图版" if variant == "rendered" else "原文件"

        # 1. 续跑:上一次「已提交但未取回」的批次先等完(不重新上传)
        task_id = self._resume_batch(cache, store, params_fp, variant, source, parts,
                                     pdf_path, sharded, work_dir)

        # 2. 只提交仍然缺失的段;已落盘的段直接复用,不重复上传、不重复 OCR
        missing = [p for p in parts if not store.load(p.part_id, params_fp)]
        failed: dict[str, str] = {}
        if missing:
            task_id, failed = self._submit_missing(pdf_path, work_dir, cache, store, params_fp,
                                                   variant, source, parts, missing, sharded)
            # 用 has_result(事实)而不是 load(策略):--no-resume 时刚下载下来的段
            # 也必须算数,否则会一边下载一边把自己判成「还缺」
            missing = [p for p in missing if not store.has_result(p.part_id, params_fp)]
        if missing:
            labels = ", ".join(p.part_id for p in missing)
            detail = next((m for p in missing if (m := failed.get(p.part_id))), "")
            raise MinerUError(
                f"云端 OCR 未完成({kind}):段 {labels} 仍缺结果"
                f"{' —— ' + detail if detail else ''}"
                f";重跑只会重试缺的段,已完成的段会直接复用",
                err_msg=detail or "incomplete", retryable=False,
            )

        result = self._merge_parts(work_dir, parts, sharded, store, total_pages,
                                   task_id=task_id)
        cache.clear()
        return result

    def _plan_parts(self, total_pages: int) -> list[OcrPart]:
        """本文件会按哪些段提交:超过单任务上限时按 page_ranges 分段,否则整本一段。

        段标识(part_id / page_range)在**跨批次**上稳定 —— 补提交某一段时它的缓存
        位置与合并位置都不变,这是「只补缺失段」能成立的前提。
        """
        ranges = self._build_page_ranges(total_pages) or [f"1-{total_pages}"]
        sharded = len(ranges) > 1
        return [
            OcrPart(idx=i, part_id=rng, page_range=rng if sharded else None)
            for i, rng in enumerate(ranges, start=1)
        ]

    @staticmethod
    def _target(part: OcrPart, pdf_path: Path, sharded: bool) -> str:
        """轮询匹配键:分片用 data_id(part-N),整本单任务用文件名(与提交接口一致)。"""
        return f"part-{part.idx}" if sharded else pdf_path.name

    def _cached_target(self, cached: dict, part: OcrPart, pdf_path: Path, sharded: bool) -> str:
        """缓存里的批次覆盖了哪一段(优先读落盘的段元信息;旧格式按 data_id 推)。"""
        for entry in cached.get("parts") or []:
            if entry.get("part_id") == part.part_id:
                return entry.get("target") or f"part-{entry.get('idx')}"
        return f"part-{part.idx}" if sharded else pdf_path.name

    def _resume_batch(self, cache: TaskCache, store: PartStore, params_fp: str, variant: str,
                      source: Path, parts: list[OcrPart], pdf_path: Path, sharded: bool,
                      work_dir: Path) -> str | None:
        """把缓存里那个「已提交但未取回」的批次等完:只轮询它覆盖的、本地还缺的段。

        返回这次用掉的批次 id(没有则 None),供结果里回填 task_id。
        """
        if not self.resume:
            return None
        cached = cache.load(source, variant)
        if not cached or not cached.get("batch_id"):
            return None
        batch_id = cached["batch_id"]
        targets_all = list(cached.get("targets") or [])
        if not targets_all:
            cache.clear()
            return None

        wanted: dict[str, OcrPart] = {}      # 轮询键 → 段
        for part in parts:
            key = self._cached_target(cached, part, pdf_path, sharded)
            if key in targets_all and store.load(part.part_id, params_fp) is None:
                wanted[key] = part
        if not wanted:
            cache.clear()                    # 这一批覆盖的段都已在本地,缓存没用了
            return None

        print(f"[mineru] 发现未取回的云端任务: batch_id={batch_id},"
              f"继续轮询 {len(wanted)} 段(不重新上传)")
        items = self._probe_batch(batch_id, list(wanted))
        if items is None:
            cache.clear()                    # 批次已失效:交给调用方重新提交缺失的段
            return None
        # 轮询若抛错(超时 / 网络 / 下载失败)**不清缓存**:那不等于批次已死,清掉它
        # 会让下一次重新上传、重新 OCR、重复计费,而云端那份结果其实还在。
        self._poll_batch(
            batch_id, list(wanted), initial_items=items,
            on_done=lambda item: self._store_item(store, params_fp, variant,
                                                  wanted.get(_item_key(item)), item, work_dir),
        )
        # 走到这里说明整批都已到终态(成功/失败都取到了结果):缓存使命结束。
        # 失败的段由调用方重新提交 —— 否则这个死批次会让每次重跑都卡在同一个地方
        cache.clear()
        return batch_id

    def _submit_missing(self, pdf_path: Path, work_dir: Path, cache: TaskCache,
                        store: PartStore, params_fp: str, variant: str, source: Path,
                        parts: list[OcrPart], missing: list[OcrPart],
                        sharded: bool) -> tuple[str | None, dict[str, str]]:
        """只提交缺失的段,提交成功立即落盘批次 id(此后中断也能续跑)。

        返回 ``(批次 id, {part_id: 云端错误信息})``,供调用方判定与报错。
        """
        task_id = self._upload(pdf_path, missing, sharded)
        targets = [self._target(p, pdf_path, sharded) for p in missing]
        print(f"[mineru] 任务已提交: batch_id={task_id} (file={pdf_path.name}, "
              f"{len(missing)}/{len(parts)} 段: {', '.join(p.part_id for p in missing)})")
        cache.save(
            source, variant,
            batch_id=task_id, targets=targets, model_version=self.model_version,
            parts=[{"part_id": p.part_id, "idx": p.idx, "range": p.page_range, "target": t}
                   for p, t in zip(missing, targets)],
        )
        if len(targets) > 1:
            events.emit("shards", total=len(targets),
                        ranges=[p.page_range for p in missing])

        lookup = {t: p for p, t in zip(missing, targets)}
        _done, failed = self._poll_batch(
            task_id, targets,
            on_done=lambda item: self._store_item(store, params_fp, variant,
                                                  lookup.get(_item_key(item)), item, work_dir),
        )
        cache.clear()      # 批次已终结;轮询抛错(超时/网络)时走不到这里,缓存保留
        return task_id, {lookup[t].part_id: msg for t, msg in failed.items() if t in lookup}

    def _count_pages(self, pdf_path: Path) -> int:
        try:
            with pymupdf.open(pdf_path) as doc:
                return doc.page_count
        except Exception as e:  # noqa: BLE001 - 页数读不出则交给上传阶段报错
            raise MinerUError(f"无法读取 PDF 页数: {pdf_path} ({e})", retryable=False) from e

    def _build_page_ranges(self, total_pages: int) -> list[str]:
        """按 max_pages_per_task 生成 1-indexed 页码范围。

        302 页 → ["1-200", "201-302"];≤ 上限时返回 [] 表示无需分片。
        """
        if total_pages <= self.max_pages_per_task:
            return []
        ranges: list[str] = []
        start = 1
        while start <= total_pages:
            end = min(start + self.max_pages_per_task - 1, total_pages)
            ranges.append(f"{start}-{end}")
            start = end + 1
        return ranges

    @staticmethod
    def _render_to_image_pdf(pdf_path: Path, work_dir: Path, dpi: int = 150, jpg_quality: int = 85) -> Path:
        """将 PDF 渲染为纯图 PDF(JPEG 压缩),供 MinerU 重试。
        部分 PDF 文字层损坏(MinerU 解析失败)但渲染正常,转为纯图后
        即可正常 OCR。JPEG 质量 85 保持文字可读且体积可控(≤200MB)。
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        out_path = work_dir / "_rendered.pdf"
        src = pymupdf.open(pdf_path)
        out = pymupdf.open()
        try:
            for page in src:
                pix = page.get_pixmap(dpi=dpi)
                img = pix.tobytes("jpeg", jpg_quality=jpg_quality)
                new_page = out.new_page(width=pix.width, height=pix.height)
                new_page.insert_image(new_page.rect, stream=img)
            out.save(out_path, garbage=3, deflate=True)
        finally:
            src.close()
            out.close()
        return out_path

    def _upload(self, pdf_path: Path, parts: list[OcrPart], sharded: bool) -> str:
        """申请上传 URL 并 PUT 上传,返回 batch_id。

        只提交传进来的这些段(首次是全部,续跑只剩缺失的),所以已完成的段不会被
        再上传一次。``sharded`` 由**整份计划**决定而不是本次段数 —— 否则补提交
        「3 段里剩下的第 2 段」会漏掉 page_ranges,把整本重新解析一遍。
        """
        if sharded:
            files = [
                {
                    "name": f"{pdf_path.stem}_part{p.idx}{pdf_path.suffix}",
                    "is_ocr": self.is_ocr,
                    "page_ranges": p.page_range,
                    "data_id": f"part-{p.idx}",
                }
                for p in parts
            ]
        else:
            files = [{"name": pdf_path.name, "is_ocr": self.is_ocr}]

        payload = {
            "files": files,
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        data = self._post_json("/file-urls/batch", payload)["data"]
        batch_id = data["batch_id"]
        file_urls = data["file_urls"]
        if len(file_urls) != len(files):
            raise MinerUError(f"上传 URL 数量不符: 期望 {len(files)}, 实际 {len(file_urls)}")

        # PUT 上传(官方要求不设置 Content-Type);分片时同一文件上传到每个条目 URL
        for url in file_urls:
            with open(pdf_path, "rb") as f:
                resp = requests.put(url, data=f, timeout=300)
            if resp.status_code not in (200, 201):
                raise MinerUError(
                    f"文件上传失败 HTTP {resp.status_code}: {resp.text[:200]}",
                    retryable=resp.status_code not in NON_RETRYABLE_HTTP,
                )
        print(f"[mineru] 上传成功: {pdf_path.name} (×{len(file_urls)})")
        return batch_id

    def _probe_batch(self, batch_id: str, targets: list[str]) -> list[dict] | None:
        """探测缓存的 batch 是否还能继续轮询(续跑用,不发上传请求)。

        返回该 batch 当前的 items(可能仍在解析中);batch 已失效 / 无权访问 /
        内容与本次不符时返回 None,由调用方回退到重新提交。

        探测**问不到**(网络抖动、服务端 5xx)时不返回 None:那不等于批次已死,丢掉
        批次 id 会让已提交的任务被重新上传、重新计费 —— 抛可重试错误,缓存留着。
        """
        try:
            data = self._get_json(f"/extract-results/batch/{batch_id}")["data"]
        except requests.RequestException as e:
            raise MinerUError(f"续跑探测失败({e}),保留缓存稍后重试") from e
        except MinerUError as e:
            if e.retryable:
                raise MinerUError(f"续跑探测失败({e}),保留缓存稍后重试") from e
            print(f"[mineru] 缓存的 batch 已不可用({e}),重新提交任务")
            return None
        except (KeyError, TypeError) as e:
            print(f"[mineru] 续跑探测失败({e}),重新提交任务")
            return None

        items = data.get("extract_result") or []
        if not items:
            print(f"[mineru] 缓存的 batch 已失效(batch_id={batch_id} 无记录),重新提交任务")
            return None
        keys = {_item_key(i) for i in items}
        if targets and not (set(targets) & keys):
            print("[mineru] 缓存的 batch 内容与本次任务不符,重新提交任务")
            return None
        return items

    def _poll_batch(
        self,
        batch_id: str,
        targets: list[str],
        initial_items: list[dict] | None = None,
        on_done=None,
    ) -> tuple[list[dict], dict[str, str]]:
        """轮询批量结果直至 targets 全部进入终态。

        返回 ``(按 targets 顺序的已成功条目, {target: 云端错误信息} 的失败条目)``。

        云端把某一段判死**不再直接抛异常**:调用方要先抢救其余已完成的段(它们已经
        计费了),再决定是只重提失败的那一段还是报错 —— 直接抛会把付过钱的段丢掉,
        重跑时再买一遍。

        targets: 分片时传 data_id(part-1/part-2/...);单任务传 [file_name]。
        匹配优先 data_id,回退 file_name(分片条目名唯一,双保险)。
        initial_items: 续跑时由 _probe_batch 已取到的首轮结果,避免重复请求。
        on_done: 某段一完成就立刻回调(下载 + 落盘),这样即使后面超时/中断,
        已经拿到的段也不会丢。
        """
        remaining = list(targets)
        collected: dict[str, dict] = {}
        failed: dict[str, str] = {}
        start = time.time()
        items = initial_items
        while time.time() - start < self.timeout:
            if items is None:
                items = self._get_json(f"/extract-results/batch/{batch_id}")["data"].get(
                    "extract_result", []
                )
            for item in items:
                key = _item_key(item)
                if key not in remaining:
                    continue
                state = item.get("state")
                if state == "done":
                    collected[key] = item
                    remaining.remove(key)
                    if on_done is not None:
                        on_done(item)          # 先落盘,再继续等其余的段
                    # 结构化进度:每收集到一个条目就报一次(前端据此显示真实百分比)
                    events.progress("extract", len(collected), len(targets), detail="mineru")
                elif state == "failed":
                    detail = str(item.get("err_msg", "未知错误"))
                    failed[key] = detail
                    remaining.remove(key)
                    print(f"[mineru] 云端解析失败({key}): {detail}")
                else:
                    progress = item.get("extract_progress") or {}
                    detail = ""
                    if progress:
                        detail = f" ({progress.get('extracted_pages')}/{progress.get('total_pages')} 页)"
                    print(f"[mineru] {STATE_LABELS.get(state, state)}{detail} ...")
            if not remaining:
                return [collected[t] for t in targets if t in collected], failed
            items = None
            time.sleep(self.poll_interval)
        # 超时是可重试的:批次 id 还在缓存里,重跑继续等这一批,不会重新上传
        raise MinerUError(f"轮询超时({self.timeout}s), batch_id={batch_id}, 未完成: {remaining}")

    def _store_item(self, store: PartStore, params_fp: str, variant: str,
                    part: OcrPart | None, item: dict, work_dir: Path) -> None:
        """下载一段的结果并**立刻落盘**:这一段已经计费了,拿到就存,别等整本完成。"""
        if part is None:
            return
        work_dir = Path(work_dir)
        slug = _safe_part_name(part.part_id)
        tmp_zip = work_dir / f"_mineru_{slug}.zip"
        extract_tmp = work_dir / f"_mineru_extract_{slug}"
        try:
            with requests.get(item["full_zip_url"], stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with open(tmp_zip, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            extract_tmp.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(extract_tmp)

            # 找到 full.md(MinerU 标准输出)
            md_candidates = sorted(extract_tmp.rglob("full.md"))
            if not md_candidates:
                raise MinerUError(f"结果包中未找到 full.md (段 {part.part_id})", retryable=False)
            full_md = md_candidates[0]
            store.save(
                part.part_id, params_fp,
                full_md.read_text(encoding="utf-8", errors="replace"),
                images_src=full_md.parent / "images",
                page_range=part.page_range, variant=variant,
            )
            print(f"[mineru] 段 {part.part_id} 结果已落盘(重跑不再重复 OCR)")
        finally:
            tmp_zip.unlink(missing_ok=True)
            shutil.rmtree(extract_tmp, ignore_errors=True)

    def _merge_parts(self, work_dir: Path, parts: list[OcrPart], sharded: bool,
                     store: PartStore, total_pages: int,
                     task_id: str | None = None) -> ConversionResult:
        """按段序合并已落盘的段结果(全部命中时完全不碰云端)。

        分片时每段按**提交时的真实 page_range**写页码注释;图片并入统一 images/,
        跨段重名加 p{段号}_ 前缀并替换引用。
        """
        work_dir = Path(work_dir)
        images_abs = work_dir / "images"
        images_abs.mkdir(parents=True, exist_ok=True)

        parts_md: list[str] = []
        img_count = 0
        for part in parts:
            md_text = store.md_path(part.part_id).read_text(encoding="utf-8", errors="replace")
            md_text, n = _copy_part_images(
                store.images_dir(part.part_id), images_abs, md_text, prefix=f"p{part.idx}_"
            )
            img_count += n
            body = md_text.strip()
            if sharded:
                body = f"{_part_marker(part.page_range, part.idx)}\n{body}"
            parts_md.append(body)

        merged = normalize_image_refs("\n\n".join(parts_md), images_abs)
        book_md = work_dir / "book.md"
        book_md.write_text(merged, encoding="utf-8")

        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            task_id=task_id,
            stats={
                "chars": len(merged),
                "images": img_count,
                "model": self.model_version,
                "parts": len(parts),
                "total_pages": total_pages,
            },
        )


def _item_key(item: dict) -> str:
    """轮询条目 → 匹配键(分片用 data_id,单任务回退文件名)。"""
    return item.get("data_id") or item.get("file_name") or ""


def _copy_part_images(src_dir: Path, dst_dir: Path, md: str, *, prefix: str) -> tuple[str, int]:
    """把某段的图片并入统一 images/,并保持「同一输入 → 同一文件名」。

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


def _part_marker(page_range: str | None, idx: int) -> str:
    """分片段落的页码注释:优先用提交时的真实 page_range,缺失时退化为段序标记。"""
    if page_range:
        return page_marker_from_range(page_range)
    return f"<!-- page-group {idx} -->"
