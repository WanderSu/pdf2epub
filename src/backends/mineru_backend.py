"""MinerU Cloud 后端(idea.md §13 / Phase 4)。

流程(官方 /api/v4 精准解析 API,本地文件上传模式):
  1. POST /api/v4/file-urls/batch  申请上传 URL + batch_id
  2. PUT  上传文件(系统自动提交解析任务)
  3. GET  /api/v4/extract-results/batch/{batch_id}  轮询至 done/failed
  4. 下载 full_zip_url → 解压(MinerU 输出:full.md + images/ + JSON)
  5. 规范化为统一 work/book.md + work/images/

凭证:MINERU_API_TOKEN 环境变量(不得硬编码)。
"""
from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import requests

from .base import Backend, ConversionResult, normalize_image_refs

DEFAULT_BASE_URL = "https://mineru.net/api/v4"
DEFAULT_TIMEOUT = 600      # 轮询总超时(秒)
POLL_INTERVAL = 5          # 轮询间隔(秒)

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
    ) -> None:
        self.token = token or os.environ.get("MINERU_API_TOKEN", "")
        if not self.token:
            raise MinerUError("缺少 MinerU API Token:请设置环境变量 MINERU_API_TOKEN")
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.is_ocr = is_ocr
        self.enable_formula = enable_formula
        self.enable_table = enable_table
        self.language = language
        self.timeout = timeout
        self.poll_interval = poll_interval

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

        task_id = self._upload(pdf_path)
        print(f"[mineru] 任务已提交: batch_id={task_id} (file={pdf_path.name})")
        result = self._poll_batch(task_id, pdf_path.name)
        print(f"[mineru] 解析完成, 下载结果...")
        return self._unpack(result["full_zip_url"], work_dir, task_id)

    def _upload(self, pdf_path: Path) -> str:
        """申请上传 URL 并 PUT 上传,返回 batch_id。"""
        payload = {
            "files": [{"name": pdf_path.name, "is_ocr": self.is_ocr}],
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        data = self._post_json("/file-urls/batch", payload)["data"]
        batch_id = data["batch_id"]
        file_urls = data["file_urls"]
        if not file_urls:
            raise MinerUError("未返回上传 URL")

        # PUT 上传(官方要求不设置 Content-Type)
        with open(pdf_path, "rb") as f:
            resp = requests.put(file_urls[0], data=f, timeout=300)
        if resp.status_code not in (200, 201):
            raise MinerUError(f"文件上传失败 HTTP {resp.status_code}: {resp.text[:200]}")
        print(f"[mineru] 上传成功: {pdf_path.name}")
        return batch_id

    def _poll_batch(self, batch_id: str, file_name: str) -> dict:
        """轮询批量结果直至目标文件 done/failed。"""
        start = time.time()
        while time.time() - start < self.timeout:
            data = self._get_json(f"/extract-results/batch/{batch_id}")["data"]
            for item in data.get("extract_result", []):
                if item.get("file_name") != file_name:
                    continue
                state = item.get("state")
                if state == "done":
                    return item
                if state == "failed":
                    raise MinerUError(f"MinerU 解析失败: {item.get('err_msg', '未知错误')}")
                progress = item.get("extract_progress", {})
                detail = ""
                if progress:
                    detail = f" ({progress.get('extracted_pages')}/{progress.get('total_pages')} 页)"
                print(f"[mineru] {STATE_LABELS.get(state, state)}{detail} ...")
            time.sleep(self.poll_interval)
        raise MinerUError(f"轮询超时({self.timeout}s), batch_id={batch_id}")

    def _unpack(self, zip_url: str, work_dir: Path, task_id: str) -> ConversionResult:
        """下载结果 zip 并规范化为 work/book.md + work/images/。"""
        work_dir.mkdir(parents=True, exist_ok=True)

        tmp_zip = work_dir / "_mineru_result.zip"
        with requests.get(zip_url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(tmp_zip, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)

        extract_tmp = work_dir / "_mineru_extract"
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(extract_tmp)

        # 找到 full.md(MinerU 标准输出)
        md_candidates = sorted(extract_tmp.rglob("full.md"))
        if not md_candidates:
            raise MinerUError("结果包中未找到 full.md")
        full_md = md_candidates[0]

        # images/ 复制到 work/images/(含中文/特殊字符文件名)
        images_abs = work_dir / "images"
        # 统一目录结构:清掉旧产物再复制
        if images_abs.is_dir():
            for old in images_abs.iterdir():
                if old.is_file():
                    old.unlink(missing_ok=True)
        images_abs.mkdir(parents=True, exist_ok=True)
        src_images = full_md.parent / "images"
        img_count = 0
        if src_images.is_dir():
            for img in sorted(src_images.iterdir()):
                if img.is_file():
                    img.replace(images_abs / img.name)
                    img_count += 1

        md_text = full_md.read_text(encoding="utf-8", errors="replace")
        md_text = normalize_image_refs(md_text, images_abs)

        book_md = work_dir / "book.md"
        book_md.write_text(md_text, encoding="utf-8")

        # 清理临时产物
        tmp_zip.unlink(missing_ok=True)
        import shutil

        shutil.rmtree(extract_tmp, ignore_errors=True)

        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            task_id=task_id,
            stats={"chars": len(md_text), "images": img_count, "model": self.model_version},
        )
