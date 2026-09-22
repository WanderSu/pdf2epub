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

凭证:MINERU_API_TOKEN 环境变量(不得硬编码)。
"""
from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path

import requests
import pymupdf

from .base import Backend, ConversionResult, TaskCache, normalize_image_refs
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


class MinerUError(RuntimeError):
    """MinerU API 错误。

    err_msg 是**云端返回的原始错误信息**:降级判定只看它,不看我们自己拼的中文前缀 ——
    否则 `MinerU 解析失败: xxx` 这类消息会让任何失败都被误判成「解析失败」而触发
    渲染纯图重试(白跑一轮、重复消耗额度)。
    """

    def __init__(self, message: str, *, err_msg: str | None = None) -> None:
        super().__init__(message)
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
                "缺少 MinerU API Token:请设置环境变量 MINERU_API_TOKEN 或项目根目录 apikey.json"
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
            raise MinerUError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("code") != 0:
            raise MinerUError(f"API 错误 code={data.get('code')} msg={data.get('msg')}")
        return data

    # ---------- 核心流程 ----------
    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        pdf_path = Path(pdf_path)
        work_dir = Path(work_dir)
        if not pdf_path.exists():
            raise MinerUError(f"文件不存在: {pdf_path}")

        try:
            result = self._convert_inner(pdf_path, work_dir, variant="original")
        except MinerUError as e:
            # 伪文字层等结构异常的文件 MinerU 会解析失败,降级为渲染纯图后重试;
            # 其他错误(网络、额度、格式不支持)直接抛 —— 重试只是白跑一轮
            if not _is_parse_failure(e.err_msg):
                raise
            print("[mineru] 原文件解析失败(可能为伪文字层),降级为渲染纯图后重试 ...")
            self._cache(work_dir).clear()   # 原文件任务已判死,缓存作废
            rendered = self._render_to_image_pdf(pdf_path, work_dir)
            try:
                result = self._convert_inner(
                    rendered, work_dir, variant="rendered", source_pdf=pdf_path
                )
                print("[mineru] 降级 OCR 成功(渲染纯图版)")
            finally:
                rendered.unlink(missing_ok=True)
        print("[mineru] 解析完成, 下载结果...")
        return result

    @staticmethod
    def _cache(work_dir: Path) -> TaskCache:
        return TaskCache(work_dir, MinerUAdapter.name)

    def _convert_inner(
        self,
        pdf_path: Path,
        work_dir: Path,
        variant: str = "original",
        source_pdf: Path | None = None,
    ) -> ConversionResult:
        """核心转换:页数 ≤ max_pages_per_task 单任务;超过则按 page_ranges 分片提交。

        variant/source_pdf:降级重试时 pdf_path 是本地渲染的纯图临时文件,
        而缓存指纹要跟着**原始 PDF**(临时文件每次重渲染字节可能不同),
        variant 则区分「原文件」与「渲染纯图」两条任务线。
        """
        source = source_pdf or pdf_path
        cache = self._cache(work_dir)

        # 0. 续跑:上次已提交但未取回结果的云端任务,直接继续轮询(不重新上传)
        cached = cache.load(source, variant) if self.resume else None
        if cached and cached.get("batch_id"):
            print(
                f"[mineru] 发现未取回的云端任务: batch_id={cached['batch_id']},"
                "继续轮询(不重新上传)"
            )
            items = self._probe_batch(cached["batch_id"], list(cached.get("targets") or []))
            if items is not None:
                items = self._poll_batch(
                    cached["batch_id"], list(cached.get("targets") or []), initial_items=items
                )
                result = self._unpack(items, work_dir, cached["batch_id"],
                                      page_ranges=cached.get("page_ranges"))
                cache.clear()
                return result
            cache.clear()

        total_pages = self._count_pages(pdf_path)
        ranges = self._build_page_ranges(total_pages)

        if not ranges:
            # 单任务(≤ 200 页),保持原有行为
            task_id = self._upload(pdf_path)
            print(f"[mineru] 任务已提交: batch_id={task_id} (file={pdf_path.name})")
            targets = [pdf_path.name]
        else:
            # 分片:同一 PDF 提交多个条目,各自指定页码范围(1-indexed)
            task_id = self._upload(pdf_path, ranges)
            print(
                f"[mineru] 任务已提交: batch_id={task_id} (file={pdf_path.name}, "
                f"{total_pages} 页超过单任务上限 {self.max_pages_per_task} 页,"
                f"自动分片 {len(ranges)} 段: {', '.join(ranges)})"
            )
            targets = [f"part-{i + 1}" for i in range(len(ranges))]

        # 提交成功立即落盘:此后不论被中断/超时,重跑都能续跑同一个任务
        cache.save(
            source, variant,
            batch_id=task_id, targets=targets, total_pages=total_pages,
            page_ranges=ranges, model_version=self.model_version,
        )
        if len(targets) > 1:
            events.emit("shards", total=len(targets), ranges=list(ranges))
        items = self._poll_batch(task_id, targets)
        result = self._unpack(items, work_dir, task_id, page_ranges=ranges)
        cache.clear()
        return result

    def _count_pages(self, pdf_path: Path) -> int:
        try:
            with pymupdf.open(pdf_path) as doc:
                return doc.page_count
        except Exception as e:  # noqa: BLE001 - 页数读不出则交给上传阶段报错
            raise MinerUError(f"无法读取 PDF 页数: {pdf_path} ({e})") from e

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

    def _upload(self, pdf_path: Path, page_ranges: list[str] | None = None) -> str:
        """申请上传 URL 并 PUT 上传,返回 batch_id。

        page_ranges 为 None → 单条目(整本);否则按「1-200」「201-302」逐段提交
        多个 files 条目(同一文件上传到每个条目对应的 URL),条目名带 _partN
        后缀 + data_id,便于轮询时区分与按段序合并。
        """
        if page_ranges:
            files = [
                {
                    "name": f"{pdf_path.stem}_part{i}{pdf_path.suffix}",
                    "is_ocr": self.is_ocr,
                    "page_ranges": rng,
                    "data_id": f"part-{i}",
                }
                for i, rng in enumerate(page_ranges, start=1)
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
                raise MinerUError(f"文件上传失败 HTTP {resp.status_code}: {resp.text[:200]}")
        print(f"[mineru] 上传成功: {pdf_path.name} (×{len(file_urls)})")
        return batch_id

    def _probe_batch(self, batch_id: str, targets: list[str]) -> list[dict] | None:
        """探测缓存的 batch 是否还能继续轮询(续跑用,不发上传请求)。

        返回该 batch 当前的 items(可能仍在解析中);batch 已失效/无权访问/
        内容与本次不符时返回 None,由调用方回退到重新提交。
        """
        try:
            data = self._get_json(f"/extract-results/batch/{batch_id}")["data"]
        except (MinerUError, requests.RequestException, KeyError, TypeError) as e:
            print(f"[mineru] 续跑探测失败({e}),将重新提交任务")
            return None
        items = data.get("extract_result") or []
        if not items:
            print(f"[mineru] 缓存的 batch 已失效(batch_id={batch_id} 无记录),重新提交任务")
            return None
        keys = {i.get("data_id") or i.get("file_name") for i in items}
        if targets and not (set(targets) & keys):
            print("[mineru] 缓存的 batch 内容与本次任务不符,重新提交任务")
            return None
        return items

    def _poll_batch(
        self,
        batch_id: str,
        targets: list[str],
        initial_items: list[dict] | None = None,
    ) -> list[dict]:
        """轮询批量结果直至 targets 全部 done/failed,按 targets 顺序返回 items。

        targets: 分片时传 data_id(part-1/part-2/...);单任务传 [file_name]。
        匹配优先 data_id,回退 file_name(分片条目名唯一,双保险)。
        initial_items: 续跑时由 _probe_batch 已取到的首轮结果,避免重复请求。
        """
        remaining = set(targets)
        collected: dict[str, dict] = {}
        start = time.time()
        items = initial_items
        while time.time() - start < self.timeout:
            if items is None:
                items = self._get_json(f"/extract-results/batch/{batch_id}")["data"].get(
                    "extract_result", []
                )
            for item in items:
                key = item.get("data_id") or item.get("file_name")
                if key not in remaining:
                    continue
                state = item.get("state")
                if state == "done":
                    collected[key] = item
                    remaining.discard(key)
                    # 结构化进度:每收集到一个条目就报一次(前端据此显示真实百分比)
                    events.progress("extract", len(collected), len(targets), detail="mineru")
                elif state == "failed":
                    detail = str(item.get("err_msg", "未知错误"))
                    raise MinerUError(f"MinerU 解析失败: {detail}", err_msg=detail)
                else:
                    progress = item.get("extract_progress", {})
                    detail = ""
                    if progress:
                        detail = f" ({progress.get('extracted_pages')}/{progress.get('total_pages')} 页)"
                    print(f"[mineru] {STATE_LABELS.get(state, state)}{detail} ...")
            if not remaining:
                return [collected[t] for t in targets]
            items = None
            time.sleep(self.poll_interval)
        raise MinerUError(f"轮询超时({self.timeout}s), batch_id={batch_id}, 未完成: {remaining}")

    def _unpack(
        self,
        items: list[dict],
        work_dir: Path,
        task_id: str,
        page_ranges: list[str] | None = None,
    ) -> ConversionResult:
        """下载结果 zip(单任务或分片多个),按段序合并为 work/book.md + work/images/。

        分片时每段独立解包,图片并入统一 images/(重名加 p{idx}_ 前缀并替换引用),
        Markdown 按段序拼接并加**真实页码**注释(``<!-- page 201-302 -->``,
        页码来自提交时的 page_ranges;缺失时退化为 ``<!-- page-group N -->``)。
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        images_abs = work_dir / "images"
        images_abs.mkdir(parents=True, exist_ok=True)

        parts_md: list[str] = []
        img_count = 0
        for idx, item in enumerate(items, start=1):
            tmp_zip = work_dir / f"_mineru_part{idx}.zip"
            with requests.get(item["full_zip_url"], stream=True, timeout=300) as resp:
                resp.raise_for_status()
                with open(tmp_zip, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 16):
                        f.write(chunk)

            extract_tmp = work_dir / f"_mineru_extract{idx}"
            with zipfile.ZipFile(tmp_zip) as zf:
                zf.extractall(extract_tmp)

            # 找到 full.md(MinerU 标准输出)
            md_candidates = sorted(extract_tmp.rglob("full.md"))
            if not md_candidates:
                raise MinerUError(f"结果包中未找到 full.md (part {idx})")
            full_md = md_candidates[0]
            md_text = full_md.read_text(encoding="utf-8", errors="replace")

            # images/ 并入统一 work/images/(含中文/特殊字符文件名)
            src_images = full_md.parent / "images"
            if src_images.is_dir():
                for img in sorted(src_images.iterdir()):
                    if not img.is_file():
                        continue
                    target = images_abs / img.name
                    if target.exists():
                        # 跨段重名:加 p{idx}_ 前缀并替换引用
                        new_name = f"p{idx}_{img.name}"
                        shutil.copy2(img, images_abs / new_name)
                        md_text = md_text.replace(f"images/{img.name}", f"images/{new_name}")
                    else:
                        shutil.copy2(img, target)
                    img_count += 1

            parts_md.append(md_text.strip() if len(items) == 1
                           else f"{_part_marker(page_ranges, idx)}\n{md_text.strip()}")
            tmp_zip.unlink(missing_ok=True)
            shutil.rmtree(extract_tmp, ignore_errors=True)

        merged = "\n\n".join(parts_md)
        merged = normalize_image_refs(merged, images_abs)

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
                "parts": len(items),
            },
        )


def _part_marker(page_ranges: list[str] | None, idx: int) -> str:
    """分片段落的页码注释:优先用提交时的真实 page_ranges,缺失时退化为段序标记。"""
    if page_ranges and 1 <= idx <= len(page_ranges):
        return page_marker_from_range(page_ranges[idx - 1])
    return f"<!-- page-group {idx} -->"
