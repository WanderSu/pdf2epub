"""PaddleOCR-VL 1.6 云端后端(idea.md §13 / Phase 5)。

对接百度 AI Studio「PaddleOCR-VL」官方 API(单 Token 鉴权):
  1. POST https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
     multipart 上传本地文件(model=PaddleOCR-VL-1.6)
  2. GET .../jobs/{jobId} 轮询至 done/failed
  3. 下载 JSONL 结果:每页 markdown.text + markdown.images{相对路径: URL}
  4. 拼接为统一 work/book.md,图片按相对路径下载到 work/images/

**脚注**:云端 `markdown_ignore_labels` 默认忽略 `footnote`,不显式传这个字段时
脚注会整批不出现在返回的 Markdown 里;这里默认传「官方默认值 − footnote」,
让脚注随正文一起返回(详见 `DEFAULT_MARKDOWN_IGNORE_LABELS`)。

**结构化结果 → 原生脚注**:结果 JSONL 每页都带
`result.layoutParsingResults[i].prunedResult.parsing_res_list[]`
(`block_label` / `block_bbox` / `block_content`),其中 `block_label == "footnote"`
就是页底脚注。这份结构化结果会归一化成与 MinerU `content_list.json` **同形**的块
(`type` / `text` / `page_idx` / `bbox`,bbox 归一化到 0-1000),随段缓存一起落盘,
再交给 `markdown.footnotes` 这套**同一个脚注引擎**做配对与渲染 —— 两个后端不各写
一套匹配逻辑(详见 `structured_blocks()` 与 `_with_footnotes()`)。

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
from markdown.footnotes import (PAGE_BREAK_TYPE, load_content_list,
                                reconstruct_footnotes)
import events

JOBS_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
MODEL = "PaddleOCR-VL-1.6"

#: 提交给云端的版面标签过滤(`optionalPayload.markdownIgnoreLabels`)。
#:
#: **为什么要显式传**:PaddleOCR-VL 的 `markdown_ignore_labels` 默认值是
#: ``['number','footnote','header','header_image','footer','footer_image','aside_text']``
#: —— 也就是说**脚注(`footnote`)默认被丢掉**,而我们此前没有传这个字段,于是
#: 脚注整批不会出现在返回的 Markdown 里(问题不在解析,在这里)。
#:
#: 默认值 = 官方默认值 **去掉 `footnote`**:页眉页脚/页码/边注这些仍然过滤
#: (保留会把 `number`/`header`/`footer` 之类的噪声全部带进正文),只让脚注留下来。
DEFAULT_MARKDOWN_IGNORE_LABELS = (
    "number",
    "header",
    "header_image",
    "footer",
    "footer_image",
    "aside_text",
)

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

#: 段缓存里附带的结构化结果文件名 —— 与 MinerU 同名同形,因此脚注层、合并逻辑、
#: 校验都共用一套(合并阶段读的就是这份「归一化块」)。
CONTENT_LIST_FILE = "content_list.json"

#: 云端 `block_label` 里代表**页底脚注**的取值 → 统一脚注层的 `page_footnote`
FOOTNOTE_BLOCK_LABELS = ("footnote",)

#: bbox 归一化尺度:PaddleOCR 给的是页面像素坐标,统一脚注层按 MinerU 的
#: 0-1000 约定理解 `bbox`(同一页内排序、页底判断都基于它)。
BBOX_SCALE = 1000.0


def _normalized_bbox(bbox, size) -> list[float] | None:
    """页面像素坐标 → 0-1000 归一化;拿不到页面尺寸就原样返回(同源比较仍然单调)。"""
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        values = [float(v) for v in bbox[:4]]
    except (TypeError, ValueError):
        return None
    if not size:
        return values
    try:
        width, height = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return values
    if width <= 0 or height <= 0:
        return values
    return [round(values[0] / width * BBOX_SCALE, 1), round(values[1] / height * BBOX_SCALE, 1),
            round(values[2] / width * BBOX_SCALE, 1), round(values[3] / height * BBOX_SCALE, 1)]


def structured_blocks(page: dict, page_idx: int, size=None) -> list[dict]:
    """把一页的云端结果归一化成与 MinerU `content_list.json` **同形**的块。

    `layoutParsingResults[i].prunedResult.parsing_res_list[]` 里每块带
    `block_label` / `block_bbox` / `block_content`:`footnote` 映射成统一脚注层的
    `page_footnote`,其余块一律当成 `text`(它们只用来给正文引用定页)。

    这里**只做形状转换**,不做任何脚注匹配 —— 匹配规则、置信度、fallback 全在
    `markdown.footnotes` 里,与 MinerU 走的是同一条路。
    """
    blocks: list[dict] = []
    for block in (page.get("prunedResult") or {}).get("parsing_res_list") or []:
        if not isinstance(block, dict):
            continue
        text = (block.get("block_content") or "").strip()
        if not text:
            continue
        label = str(block.get("block_label") or "")
        blocks.append({
            "type": "page_footnote" if label in FOOTNOTE_BLOCK_LABELS else "text",
            "text": text,
            "page_idx": page_idx,
            "bbox": _normalized_bbox(block.get("block_bbox"), size),
            "label": label,                                   # 原始标签,排查用
        })
    return blocks


def reconstruct_footnotes_in(md_text: str, blocks: list[dict]) -> tuple[str, dict]:
    """把 PaddleOCR 的 Markdown + 归一化块交给**统一脚注引擎**,返回 (新 Markdown, 统计)。

    与 MinerU 用的是同一个入口(`markdown.footnotes.reconstruct_footnotes`):
    配对规则、置信度、fallback、编号全在那里,两个后端不各写一套。「Markdown 里本来
    就带着脚注文本」这件事也由引擎统一处理(`strip_note_lines`),适配器不做特殊分支。
    """
    if not md_text or not blocks:
        return md_text, {}
    result = reconstruct_footnotes(md_text, blocks)
    return result.md, result.stats


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
        markdown_ignore_labels: list[str] | tuple[str, ...] | None = None,
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
        # None → 用默认值(保留脚注);显式传 [] 表示「什么都不过滤」
        self.markdown_ignore_labels = list(
            DEFAULT_MARKDOWN_IGNORE_LABELS if markdown_ignore_labels is None
            else markdown_ignore_labels
        )
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.resume = resume

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _params_fp(self) -> str:
        """OCR 参数指纹:换模型/开关/标签过滤后不能复用旧结果。

        `markdown_ignore_labels` **必须进指纹**:「忽略 footnote」与「保留 footnote」
        是两次不同的 OCR,复用旧结果会让配置改动看起来没生效(拿到的仍是旧文本)。
        """
        return params_fingerprint({
            "model": MODEL,
            "chart": self.use_chart_recognition,
            "orientation": self.use_doc_orientation_classify,
            "unwarp": self.use_doc_unwarping,
            "ignore_labels": self.markdown_ignore_labels,
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
        conv, blocks = self._download_result(result, work_dir, job_id)
        # 结果落盘:此后任何重跑都不再提交云端任务。结构化结果(归一化块)一并落盘 ——
        # 脚注重建在**合并/复用时**做,所以缓存里存的是未加脚注的原始 Markdown。
        extra = self._save_structured(work_dir, blocks)
        store.save(
            self.PART_ID, params_fp,
            conv.book_md.read_text(encoding="utf-8", errors="replace"),
            images_src=work_dir / "images", model=MODEL,
            extra_files=extra,
        )
        for path in (extra or {}).values():                 # 临时文件已复制进段缓存
            Path(path).unlink(missing_ok=True)
        # 脚注:读回缓存里的结构化结果,交给统一脚注引擎(与复用路径同一个函数)
        md_text, fn_stats = self._with_footnotes(store)
        conv.book_md.write_text(md_text, encoding="utf-8")
        conv.stats.update(fn_stats)
        cache.clear()
        return conv

    def _save_structured(self, work_dir: Path, blocks: list[dict]) -> dict[str, Path] | None:
        """把归一化块写成随段缓存的附带文件(没有结构化结果就不产生文件)。"""
        if not blocks:
            return None
        path = work_dir / f"_paddleocr_{CONTENT_LIST_FILE}"
        path.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
        return {CONTENT_LIST_FILE: path}

    def _with_footnotes(self, store: PartStore) -> tuple[str, dict]:
        """读段缓存里的 Markdown + 结构化结果 → 重建脚注,返回 (Markdown, 统计)。

        老缓存(本改动之前落的段)里没有 `content_list.json` —— 那段重建不出脚注,
        但**不因此判定缓存失效**:为一个附带文件让整本重新上传、重新计费不划算。
        """
        md_text = store.md_path(self.PART_ID).read_text(encoding="utf-8", errors="replace")
        path = store.dir_for(self.PART_ID) / CONTENT_LIST_FILE
        if not path.is_file():
            return md_text, {}
        md_text, stats = reconstruct_footnotes_in(md_text, load_content_list(path))
        if stats.get("notes"):
            print(f"[paddleocr] 脚注: 结构化结果 {stats['notes']} 条 → "
                  f"关联 {stats['linked']} 条 (high {stats['high']} / "
                  f"medium {stats['medium']}),未匹配 {stats['unmatched']} 条保留为普通文本")
        if stats.get("unmatched"):
            # 不静默:没配上的脚注内容仍在,只是没变成可点击脚注(与 MinerU 侧一致)
            events.emit("warning", code="footnotes_unmatched",
                        message=f"{stats['unmatched']} 条脚注未能确认对应的正文引用,"
                                f"已保留为普通文本(不做错误链接)")
        return md_text, ({"footnotes_total": stats["notes"],
                          "footnotes_linked": stats["linked"],
                          "footnotes_unmatched": stats["unmatched"]} if stats else {})

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
        # 脚注重建在复用时做(缓存里存的是未加脚注的原始 Markdown)→ 规则改进了,
        # 老缓存不用重新 OCR 也能得到新结果
        md_text, fn_stats = self._with_footnotes(store)
        book_md.write_text(md_text, encoding="utf-8")
        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            task_id=None,
            stats={"chars": len(md_text),
                   "images": sum(1 for _ in images_abs.iterdir()),
                   "model": MODEL, "total_pages": None, "cached": True, **fn_stats},
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
            # 云端默认会忽略 footnote(脚注整批消失);显式给出过滤列表,保留脚注。
            # 字段名/取值形态取自官方 API(optionalPayload.markdownIgnoreLabels)。
            "markdownIgnoreLabels": self.markdown_ignore_labels,
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

    def _download_result(self, data: dict, work_dir: Path, job_id: str
                         ) -> tuple[ConversionResult, list[dict]]:
        """下载 JSONL 结果:拼接 markdown、保存图片、归一化结构化块。

        结构化块(`parsing_res_list` → 统一块)是脚注重建的唯一数据源,和 MinerU 用
        `content_list.json` 是同一份东西。
        """
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
        page_indices: list[int] = []                    # 每页的 page_idx(与 pages_md 同步)
        img_count = 0
        blocks: list[dict] = []                         # 归一化结构化块(脚注数据源)
        page_idx = 0
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                result = json.loads(line)["result"]
            except (json.JSONDecodeError, KeyError):
                continue
            sizes = [p for p in (result.get("dataInfo") or {}).get("pages") or []
                     if isinstance(p, dict)]
            for i, res in enumerate(result.get("layoutParsingResults", [])):
                # 结构化块(页底脚注只有这里有):归一化成 unified 块交给脚注引擎
                size = ((sizes[i].get("width"), sizes[i].get("height"))
                        if i < len(sizes) else None)
                blocks.extend(structured_blocks(res, page_idx, size))
                page_idx += 1
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
                    # 先按页归一化图片引用,再拼接 —— 这样每页在最终 Markdown 里的
                    # 起始偏移就是准的(拼接后再改文本会让偏移整体错位)
                    pages_md.append(normalize_image_refs(md_part.strip(), images_abs))
                    page_indices.append(page_idx - 1)

        # 逐页拼接,同时记下每页的起始偏移 → 作为「页边界」块交给脚注层,
        # 页归属不再靠文本对齐去猜(重复页眉/页脚会把它带偏)
        md_text = ""
        for page, part in zip(page_indices, pages_md):
            if md_text:
                md_text += "\n\n"
            blocks.append({"type": PAGE_BREAK_TYPE, "page_idx": page, "offset": len(md_text)})
            md_text += part

        book_md = work_dir / "book.md"
        book_md.write_text(md_text, encoding="utf-8")
        return (
            ConversionResult(
                book_md=book_md,
                images_dir=images_abs,
                backend=self.name,
                task_id=job_id,
                stats={"chars": len(md_text), "images": img_count, "model": MODEL},
            ),
            blocks,
        )
