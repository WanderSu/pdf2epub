"""PaddleOCR-VL 1.6 云端后端(idea.md §13 / Phase 5)。

对接百度 AI Studio「PaddleOCR-VL」官方 API(单 Token 鉴权):
  1. POST https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
     multipart 上传本地文件(model=PaddleOCR-VL-1.6)
  2. GET .../jobs/{jobId} 轮询至 done/failed
  3. 下载 JSONL 结果:每页 markdown.text + markdown.images{相对路径: URL}
  4. 拼接为统一 work/book.md,图片按相对路径下载到 work/images/

**续跑的两层缓存**:`.ocr_task.json` 记「已提交但没取回结果」的 jobId(重跑继续轮询
同一个 job,不重新上传);`_parts/whole/` 记**已经拿到的结果**(云端任务成功即已计费,
之后清理/EPUB 阶段失败后的重试、用户手动重跑都直接复用它,完全不碰云端)。

凭证:PADDLEOCR_TOKEN 环境变量(不得硬编码)。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from .base import (
    Backend,
    BackendError,
    ConversionResult,
    PartStore,
    TaskCache,
    file_fingerprint,
    normalize_image_refs,
    params_fingerprint,
)
from paths import load_api_key

JOBS_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL = "PaddleOCR-VL-1.6"

DEFAULT_TIMEOUT = 600      # 轮询总超时(秒)
POLL_INTERVAL = 6          # 轮询间隔(秒)

#: 客户端类 HTTP 状态码:认证/参数/配额问题,重试改变不了结果
NON_RETRYABLE_HTTP = {400, 401, 403, 404, 405, 409, 413, 422, 429}

#: 结果 Markdown 里指向本地图片的引用(markdown 与 HTML 两种写法)
IMAGE_REF_RE = re.compile(r"""(?:!\[[^\]]*\]\(|src=["']?)images/([^)\s"'>]+)""")

STATE_LABELS = {
    "pending": "排队中",
    "running": "解析中",
    "done": "完成",
    "failed": "失败",
}


class PaddleOCRError(BackendError):
    """PaddleOCR-VL API 错误。"""


class PaddleOCRAdapter(Backend):
    name = "paddleocr"

    #: 本后端一次提交整本文件,段标识固定
    PART_ID = "whole"

    def __init__(
        self,
        token: str | None = None,
        use_chart_recognition: bool = False,
        use_doc_orientation_classify: bool = False,
        use_doc_unwarping: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
        poll_interval: int = POLL_INTERVAL,
        resume: bool = True,
    ) -> None:
        self.token = (
            token
            or os.environ.get("PADDLEOCR_TOKEN", "")
            or load_api_key("PaddleOCR-VL")
            or ""
        )
        if not self.token:
            raise PaddleOCRError(
                "缺少 PaddleOCR-VL Token:请设置环境变量 PADDLEOCR_TOKEN 或项目根目录 apikey.json",
                retryable=False,
            )
        self.use_chart_recognition = use_chart_recognition
        self.use_doc_orientation_classify = use_doc_orientation_classify
        self.use_doc_unwarping = use_doc_unwarping
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.resume = resume

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _params_fp(self) -> str:
        """OCR 参数指纹:换模型/开关后不能复用旧结果。"""
        return params_fingerprint({
            "model": MODEL,
            "chart": self.use_chart_recognition,
            "orientation": self.use_doc_orientation_classify,
            "unwarp": self.use_doc_unwarping,
        })

    # ---------- 核心流程 ----------
    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        pdf_path = Path(pdf_path)
        work_dir = Path(work_dir)
        if not pdf_path.exists():
            raise PaddleOCRError(f"文件不存在: {pdf_path}", retryable=False)

        cache = TaskCache(work_dir, PaddleOCRAdapter.name)
        params_fp = self._params_fp()
        store = PartStore(work_dir, self.name, file_fingerprint(pdf_path), resume=self.resume)

        # 结果已落盘 → 直接复用(云端那次已经计费了,重跑不该再买一次)
        reused = self._restore_result(store, work_dir, params_fp)
        if reused is not None:
            print("[paddleocr] 复用已完成的解析结果(不重新提交云端任务)")
            cache.clear()
            return reused

        # 续跑:上次已提交但未取回结果的 jobId,继续轮询(不重新上传)
        cached = cache.load(pdf_path) if self.resume else None
        job_id = cached.get("job_id") if cached else None
        if job_id:
            print(f"[paddleocr] 发现未取回的云端任务: jobId={job_id},继续轮询(不重新上传)")
            if self._probe(job_id) is None:
                cache.clear()
                job_id = None

        if not job_id:
            job_id = self._submit(pdf_path)
            print(f"[paddleocr] 任务已提交: jobId={job_id}")
            cache.save(pdf_path, "original", job_id=job_id, model=MODEL)

        result = self._poll(job_id)
        print("[paddleocr] 解析完成, 下载结果...")
        conv = self._download_result(result, work_dir, job_id)
        # 结果落盘:此后任何重跑都不再提交云端任务
        store.save(
            self.PART_ID, params_fp,
            conv.book_md.read_text(encoding="utf-8", errors="replace"),
            images_src=work_dir / "images", model=MODEL,
        )
        cache.clear()
        return conv

    def _restore_result(self, store: PartStore, work_dir: Path,
                        params_fp: str) -> ConversionResult | None:
        """复用它已落盘的结果;图片不全时返回 None(宁可重跑,也不要缺图的交付物)。"""
        if store.load(self.PART_ID, params_fp) is None:
            return None
        md_text = store.md_path(self.PART_ID).read_text(encoding="utf-8", errors="replace")
        images_abs = work_dir / "images"
        images_abs.mkdir(parents=True, exist_ok=True)
        part_images = store.images_dir(self.PART_ID)

        missing = [name for name in IMAGE_REF_RE.findall(md_text)
                   if not (images_abs / name).is_file() and not (part_images / name).is_file()]
        if missing:
            print(f"[paddleocr] 缓存结果缺少 {len(missing)} 张图片,重新提交云端任务")
            return None

        if part_images.is_dir():
            for img in sorted(part_images.iterdir()):
                if img.is_file():
                    target = images_abs / img.name
                    if not target.exists():
                        target.write_bytes(img.read_bytes())

        book_md = work_dir / "book.md"
        book_md.write_text(md_text, encoding="utf-8")
        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            task_id=None,
            stats={"chars": len(md_text),
                   "images": sum(1 for _ in images_abs.iterdir()),
                   "model": MODEL, "total_pages": None, "cached": True},
        )

    def _probe(self, job_id: str) -> dict | None:
        """探测缓存的 job 是否还能继续轮询(续跑用)。失效返回 None。

        网络/服务端故障时抛可重试错误而不是返回 None:那不等于 job 已死,丢掉 jobId
        会让已提交的任务被重新上传、重新计费。
        """
        try:
            resp = requests.get(f"{JOBS_URL}/{job_id}", headers=self._headers, timeout=60)
        except requests.RequestException as e:
            raise PaddleOCRError(f"续跑探测失败({e}),保留缓存稍后重试") from e
        if resp.status_code != 200:
            if resp.status_code not in NON_RETRYABLE_HTTP:
                raise PaddleOCRError(f"续跑探测失败(HTTP {resp.status_code}),保留缓存稍后重试")
            print(f"[paddleocr] 缓存的 job 已失效(HTTP {resp.status_code}),重新提交任务")
            return None
        data = resp.json().get("data", {}) or {}
        if data.get("state") == "failed":
            print("[paddleocr] 缓存的 job 已失败,重新提交任务")
            return None
        return data

    def _submit(self, pdf_path: Path) -> str:
        """multipart 上传文件,返回 jobId。"""
        optional_payload = {
            "useDocOrientationClassify": self.use_doc_orientation_classify,
            "useDocUnwarping": self.use_doc_unwarping,
            "useChartRecognition": self.use_chart_recognition,
        }
        data = {
            "model": MODEL,
            "optionalPayload": json.dumps(optional_payload),
        }
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                JOBS_URL,
                headers=self._headers,
                data=data,
                files={"file": (pdf_path.name, f)},
                timeout=300,
            )
        if resp.status_code != 200:
            raise PaddleOCRError(
                f"提交失败 HTTP {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code not in NON_RETRYABLE_HTTP,
            )
        job_id = resp.json().get("data", {}).get("jobId")
        if not job_id:
            raise PaddleOCRError(f"提交响应异常: {resp.text[:300]}", retryable=False)
        return job_id

    def _poll(self, job_id: str) -> dict:
        """轮询任务状态,返回 data 字典(含 resultUrl)。"""
        start = time.time()
        while time.time() - start < self.timeout:
            resp = requests.get(f"{JOBS_URL}/{job_id}", headers=self._headers, timeout=60)
            if resp.status_code != 200:
                raise PaddleOCRError(
                    f"查询失败 HTTP {resp.status_code}: {resp.text[:300]}",
                    retryable=resp.status_code not in NON_RETRYABLE_HTTP,
                )
            data = resp.json().get("data", {})
            state = data.get("state")
            if state == "done":
                return data
            if state == "failed":
                # 整本一个任务:重试 = 重新提交整本、重新计费,不值得自动重试
                raise PaddleOCRError(
                    f"PaddleOCR 解析失败: {data.get('errorMsg', '未知错误')}",
                    retryable=False,
                )
            progress = data.get("extractProgress") or {}
            detail = ""
            if progress:
                detail = f" ({progress.get('extractedPages')}/{progress.get('totalPages')} 页)"
            print(f"[paddleocr] {STATE_LABELS.get(state, state)}{detail} ...")
            time.sleep(self.poll_interval)
        # 超时可重试:jobId 还在缓存里,重跑继续轮询同一个任务,不重新上传
        raise PaddleOCRError(f"轮询超时({self.timeout}s), jobId={job_id}")

    def _download_result(self, data: dict, work_dir: Path, job_id: str) -> ConversionResult:
        """下载 JSONL 结果,拼接 markdown 并保存图片。"""
        jsonl_url = (data.get("resultUrl") or {}).get("jsonUrl")
        if not jsonl_url:
            raise PaddleOCRError("结果中缺少 jsonUrl")

        resp = requests.get(jsonl_url, timeout=300)
        resp.raise_for_status()

        work_dir.mkdir(parents=True, exist_ok=True)
        images_abs = work_dir / "images"
        # 统一目录结构(idea.md §6):清掉旧产物(上次转换的图片、imgs/ 等)
        for old_dir in (work_dir / "images", work_dir / "imgs"):
            if old_dir.is_dir():
                for old in old_dir.iterdir():
                    if old.is_file():
                        old.unlink(missing_ok=True)
        images_abs.mkdir(parents=True, exist_ok=True)

        pages_md: list[str] = []
        img_count = 0
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                result = json.loads(line)["result"]
            except (json.JSONDecodeError, KeyError):
                continue
            for res in result.get("layoutParsingResults", []):
                md_part = (res.get("markdown") or {}).get("text", "")
                # 图片:{相对路径: URL} → 下载到统一的 work/images/(取文件名)
                img_map = (res.get("markdown") or {}).get("images") or {}
                url_to_rel = {}
                for rel_path, url in img_map.items():
                    name = Path(rel_path).name
                    target = images_abs / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        img_resp = requests.get(url, timeout=120)
                        img_resp.raise_for_status()
                        target.write_bytes(img_resp.content)
                        url_to_rel[url] = f"images/{name}"
                        img_count += 1
                    except requests.RequestException as e:
                        print(f"[paddleocr] 图片下载失败 {url[:80]}: {e}")
                # markdown 中 URL 引用 → 本地相对路径
                for url, rel in url_to_rel.items():
                    md_part = md_part.replace(url, rel)
                # HTML <img src="imgs/xxx"> / ![](imgs/xxx) → images/xxx
                md_part = re.sub(
                    r'(["(\s])imgs/', r"\1images/", md_part
                )
                if md_part.strip():
                    pages_md.append(md_part.strip())

        md_text = "\n\n".join(pages_md)
        md_text = normalize_image_refs(md_text, images_abs)

        book_md = work_dir / "book.md"
        book_md.write_text(md_text, encoding="utf-8")
        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            task_id=job_id,
            stats={"chars": len(md_text), "images": img_count, "model": MODEL},
        )
