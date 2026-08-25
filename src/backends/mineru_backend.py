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

from .base import Backend, ConversionResult, normalize_image_refs
from paths import load_api_key

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
    """MinerU API 错误。"""


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
            result = self._convert_inner(pdf_path, work_dir)
        except MinerUError as e:
            # 伪文字层等结构异常的文件 MinerU 会解析失败,降级为渲染纯图后重试
            if "parsing failed" not in str(e) and "解析失败" not in str(e):
                raise
            print("[mineru] 原文件解析失败(可能为伪文字层),降级为渲染纯图后重试 ...")
            rendered = self._render_to_image_pdf(pdf_path, work_dir)
            try:
                result = self._convert_inner(rendered, work_dir)
                print("[mineru] 降级 OCR 成功(渲染纯图版)")
            finally:
                rendered.unlink(missing_ok=True)
        print("[mineru] 解析完成, 下载结果...")
        return result

    def _convert_inner(self, pdf_path: Path, work_dir: Path) -> ConversionResult:
        """核心转换:页数 ≤ max_pages_per_task 单任务;超过则按 page_ranges 分片提交。"""
        total_pages = self._count_pages(pdf_path)
        ranges = self._build_page_ranges(total_pages)

        if not ranges:
            # 单任务(≤ 200 页),保持原有行为
            task_id = self._upload(pdf_path)
            print(f"[mineru] 任务已提交: batch_id={task_id} (file={pdf_path.name})")
            items = self._poll_batch(task_id, [pdf_path.name])
        else:
            # 分片:同一 PDF 提交多个条目,各自指定页码范围(1-indexed)
            task_id = self._upload(pdf_path, ranges)
            print(
                f"[mineru] 任务已提交: batch_id={task_id} (file={pdf_path.name}, "
                f"{total_pages} 页超过单任务上限 {self.max_pages_per_task} 页,"
                f"自动分片 {len(ranges)} 段: {', '.join(ranges)})"
            )
            targets = [f"part-{i + 1}" for i in range(len(ranges))]
            items = self._poll_batch(task_id, targets)
        return self._unpack(items, work_dir, task_id)

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

    def _poll_batch(self, batch_id: str, targets: list[str]) -> list[dict]:
        """轮询批量结果直至 targets 全部 done/failed,按 targets 顺序返回 items。

        targets: 分片时传 data_id(part-1/part-2/...);单任务传 [file_name]。
        匹配优先 data_id,回退 file_name(分片条目名唯一,双保险)。
        """
        remaining = set(targets)
        collected: dict[str, dict] = {}
        start = time.time()
        while time.time() - start < self.timeout:
            data = self._get_json(f"/extract-results/batch/{batch_id}")["data"]
            for item in data.get("extract_result", []):
                key = item.get("data_id") or item.get("file_name")
                if key not in remaining:
                    continue
                state = item.get("state")
                if state == "done":
                    collected[key] = item
                    remaining.discard(key)
                elif state == "failed":
                    raise MinerUError(f"MinerU 解析失败: {item.get('err_msg', '未知错误')}")
                else:
                    progress = item.get("extract_progress", {})
                    detail = ""
                    if progress:
                        detail = f" ({progress.get('extracted_pages')}/{progress.get('total_pages')} 页)"
                    print(f"[mineru] {STATE_LABELS.get(state, state)}{detail} ...")
            if not remaining:
                return [collected[t] for t in targets]
            time.sleep(self.poll_interval)
        raise MinerUError(f"轮询超时({self.timeout}s), batch_id={batch_id}, 未完成: {remaining}")

    def _unpack(self, items: list[dict], work_dir: Path, task_id: str) -> ConversionResult:
        """下载结果 zip(单任务或分片多个),按段序合并为 work/book.md + work/images/。

        分片时每段独立解包,图片并入统一 images/(重名加 p{idx}_ 前缀并替换引用),
        Markdown 按段序拼接并加 <!-- page-group N --> 页标记(与 hybrid 流程一致)。
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
                           else f"<!-- page-group {idx} -->\n{md_text.strip()}")
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
