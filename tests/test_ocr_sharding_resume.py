"""MinerU 分片与续跑集成测试(v0.3.2 P0-2)。

目标:把「>200 页自动分片」「段序合并」「中断后复用已提交任务」「--no-resume」
这些**只有真实云端才会暴露**的行为,搬到离线、可重复的测试里。

做法:用假云端替换 `mineru_backend.requests` —— 一个记录调用并吐出预设状态的
假 HTTP 层(提交/上传/轮询/下载全走它,包括真的解一个真 zip),所以被测的是
`MinerUAdapter` 的完整流程,而不是被 mock 掉的内部函数。

约定:测试里**不许出现真实网络调用**;需要「云端中断」时用 `timeout=0` 模拟。
"""
from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
import requests

import convert
from backends import mineru_backend
from backends.base import ConversionResult
from backends.mineru_backend import MinerUAdapter, MinerUError
from conftest import make_mixed_pdf, make_paged_pdf, write_png
from page_result import page_marks


# ---------------------------------------------------------------- 假云端

class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None,
                 content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        self._content = content

    def json(self) -> dict:
        return self._payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1 << 16):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeMinerUCloud:
    """假 MinerU:按提交内容建 batch,按脚本决定轮询状态,下载返回真 zip。

    - `pending_polls`:前 N 次轮询返回 running(模拟排队/解析中)
    - `fail_batches`:前 N 个 batch 的条目直接 failed(模拟云端解析失败)
    - `fail_msg`:失败时返回的 err_msg
    - `stale`:所有轮询都返回空列表(模拟缓存里的 batch 已失效/无权限)
    """

    def __init__(self, *, pending_polls: int = 0, fail_batches: int = 0,
                 fail_msg: str = "random failure", stale: bool = False) -> None:
        self.pending_polls = pending_polls
        self.fail_batches = fail_batches
        self.fail_msg = fail_msg
        self.stale = stale
        self.batches: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.uploads: list[str] = []
        self.polls = 0

    # --- requests 的替身 ---
    def post(self, url: str, *, headers=None, json=None, timeout=None) -> FakeResponse:
        payload = json or {}
        batch_id = f"batch-{len(self.batches) + 1}"
        entries = list(payload.get("files", []))
        self.posts.append({"url": url, "payload": copy.deepcopy(payload), "batch_id": batch_id})
        self.batches[batch_id] = {"entries": entries}
        return FakeResponse(payload={
            "code": 0, "msg": "ok",
            "data": {
                "batch_id": batch_id,
                "file_urls": [f"https://upload.local/{batch_id}/{i}" for i in range(len(entries))],
            },
        })

    def put(self, url: str, *, data=None, timeout=None) -> FakeResponse:
        self.uploads.append(url)
        if hasattr(data, "read"):          # 确认上传真的发了文件内容
            assert data.read()
        return FakeResponse(status_code=200)

    def get(self, url: str, *, headers=None, stream=False, timeout=None) -> FakeResponse:
        if url.startswith("https://download.local/"):
            _batch_id, data_id = url.rstrip("/").rsplit("/", 2)[-2:]
            return FakeResponse(content=self._zip_bytes(data_id))
        return self._batch_result(url.rstrip("/").rsplit("/", 1)[-1])

    # --- 内部 ---
    def _batch_result(self, batch_id: str) -> FakeResponse:
        self.polls += 1
        batch = self.batches.get(batch_id)
        if batch is None or self.stale:
            return FakeResponse(payload={"code": 0, "data": {"extract_result": []}})
        pending = self.polls <= self.pending_polls
        failing = int(batch_id.split("-")[1]) <= self.fail_batches
        items = []
        for entry in batch["entries"]:
            item = {
                "data_id": entry.get("data_id") or entry["name"],
                "file_name": entry["name"],
                "state": "running" if pending else "done",
            }
            if not pending:
                if failing:
                    item["state"] = "failed"
                    item["err_msg"] = self.fail_msg
                else:
                    item["full_zip_url"] = f"https://download.local/{batch_id}/{item['data_id']}"
            items.append(item)
        return FakeResponse(payload={"code": 0, "data": {"extract_result": items}})

    @staticmethod
    def _zip_bytes(data_id: str) -> bytes:
        """每个分段固定产出:full.md + images/fig.png(各段同名,用于测重名处理)。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{data_id}/full.md",
                        f"# 分段 {data_id}\n\n这是 {data_id} 的正文段落,以句号结尾。\n\n"
                        f"![图](images/fig.png)\n")
            zf.writestr(f"{data_id}/images/fig.png", b"\x89PNG-fake")
        return buf.getvalue()


class _RequestsShim:
    """把 mineru_backend 里的 requests 换成假云端(保留 RequestException)。"""

    RequestException = requests.RequestException

    def __init__(self, cloud: FakeMinerUCloud) -> None:
        self.cloud = cloud

    def post(self, url, **kw):
        return self.cloud.post(url, **kw)

    def put(self, url, **kw):
        return self.cloud.put(url, **kw)

    def get(self, url, **kw):
        return self.cloud.get(url, **kw)


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> FakeMinerUCloud:
    """假云端(**单个实例**跨多次 convert 复用 —— 续跑用例要看到上一次的提交记录)。

    需要「排队中 / 解析失败 / 缓存失效」等脚本时,直接改实例属性:
    `cloud.pending_polls = 2`、`cloud.fail_batches = 1`、`cloud.stale = True`。
    """
    monkeypatch.setattr(mineru_backend.time, "sleep", lambda *_: None)
    fake = FakeMinerUCloud()
    monkeypatch.setattr(mineru_backend, "requests", _RequestsShim(fake))
    return fake


def _adapter(**kwargs) -> MinerUAdapter:
    kwargs.setdefault("poll_interval", 0)
    kwargs.setdefault("timeout", 30)
    return MinerUAdapter(token="fake-token", **kwargs)


# ---------------------------------------------------------------- 分片

def test_pages_over_limit_are_sharded_by_page_ranges(tmp_path, cloud) -> None:
    """450 页 → ceil(450/200) = 3 段,各带 1-indexed page_ranges,且每段都上传。"""
    pdf = make_paged_pdf(tmp_path / "大书.pdf", pages=450)

    result = _adapter(max_pages_per_task=200).convert(pdf, tmp_path / "work")

    files = cloud.posts[0]["payload"]["files"]
    assert [f["page_ranges"] for f in files] == ["1-200", "201-400", "401-450"]
    assert [f["data_id"] for f in files] == ["part-1", "part-2", "part-3"]
    assert len(cloud.uploads) == 3                 # 每个条目一个上传 URL
    assert result.stats["parts"] == 3


def test_shards_merge_in_order_with_real_page_markers(tmp_path, cloud) -> None:
    """分段结果按段序合并,页码注释用真实 page_ranges(而不是段号)。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=9)

    result = _adapter(max_pages_per_task=2).convert(pdf, tmp_path / "work")

    assert len(cloud.posts[0]["payload"]["files"]) == 5     # 9 页 / 每段 2 页 → 5 段
    md = result.book_md.read_text(encoding="utf-8")
    # 单页段写成 `<!-- page 9 -->`(不写 9-9),与 page_result.format_pages 一致
    assert page_marks(md) == [[1, 2], [3, 4], [5, 6], [7, 8], [9]]
    assert md.index("分段 part-1") < md.index("分段 part-2") < md.index("分段 part-5")


def test_cross_part_image_name_collision_is_renamed_and_refs_fixed(tmp_path, cloud) -> None:
    """各段都有同名图 → 后到的加 p{段号}_ 前缀,引用同步改掉,清理器不报缺失。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)

    result = _adapter(max_pages_per_task=2).convert(pdf, tmp_path / "work")

    names = sorted(p.name for p in result.images_dir.iterdir())
    assert names == ["fig.png", "p2_fig.png"]
    md = result.book_md.read_text(encoding="utf-8")
    assert "](images/fig.png)" in md and "](images/p2_fig.png)" in md
    assert all((result.images_dir / ref.split("/")[1]).exists()
               for ref in ("images/fig.png", "images/p2_fig.png"))


def test_single_task_keeps_no_page_markers(tmp_path, cloud) -> None:
    """≤200 页时不加分段页码注释(保持既有产物形态)。"""
    pdf = make_paged_pdf(tmp_path / "小书.pdf", pages=3)

    result = _adapter().convert(pdf, tmp_path / "work")

    assert "page_ranges" not in cloud.posts[0]["payload"]["files"][0]
    assert len(cloud.uploads) == 1
    assert page_marks(result.book_md.read_text(encoding="utf-8")) == []
    assert result.stats["parts"] == 1


# ---------------------------------------------------------------- 续跑

def _interrupted_run(pdf: Path, work: Path) -> str:
    """用 timeout=0 模拟「提交成功但没等到结果就中断」,返回那次提交的 batch_id。

    提交与落盘发生在真正轮询之前,所以 timeout=0 能精确复现「任务已在云端、
    本机只留下缓存」这个续跑起点。
    """
    with pytest.raises(MinerUError):
        _adapter(timeout=0).convert(pdf, work)
    cache = json.loads((work / ".ocr_task.json").read_text(encoding="utf-8"))
    return cache["batch_id"]


def test_interrupted_run_leaves_resume_cache(tmp_path, cloud) -> None:
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)
    work = tmp_path / "work"

    batch_id = _interrupted_run(pdf, work)

    cache = json.loads((work / ".ocr_task.json").read_text(encoding="utf-8"))
    assert cache["batch_id"] == batch_id
    assert cache["backend"] == "mineru"
    assert cache["variant"] == "original"
    assert cache["fingerprint"]                      # 内容指纹已落盘


def test_resume_reuses_batch_without_reupload(tmp_path, cloud) -> None:
    """续跑复用已提交任务:不再提交、不再上传,成功后清除缓存。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)
    work = tmp_path / "work"
    batch_id = _interrupted_run(pdf, work)
    posts_before, uploads_before = len(cloud.posts), len(cloud.uploads)

    result = _adapter().convert(pdf, work)

    assert len(cloud.posts) == posts_before           # 没有第二次提交
    assert len(cloud.uploads) == uploads_before       # 没有第二次上传
    assert result.task_id == batch_id
    assert not (work / ".ocr_task.json").exists()     # 取回结果后缓存清除


def test_stale_cache_is_discarded_and_resubmitted(tmp_path, cloud) -> None:
    """缓存里的 batch 已失效(探测返回空)→ 立即重提,不傻等超时。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)
    work = tmp_path / "work"
    _interrupted_run(pdf, work)
    cloud.stale = True                                # 探测一律返回空

    with pytest.raises(MinerUError):
        _adapter(timeout=0).convert(pdf, work)        # 重提之后仍等不到结果(模拟)

    assert len(cloud.posts) == 2                      # 重新提交了一次
    assert len(cloud.uploads) == 2


def test_no_resume_forces_resubmit(tmp_path, cloud) -> None:
    """--no-resume:即使缓存有效也重新提交(桌面端/CLI 开关的落地行为)。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)
    work = tmp_path / "work"
    batch_id = _interrupted_run(pdf, work)

    result = _adapter(resume=False).convert(pdf, work)

    assert len(cloud.posts) == 2
    assert result.task_id != batch_id


def test_resume_cache_is_invalidated_by_changed_file(tmp_path, cloud) -> None:
    """文件内容变了(指纹不同)→ 缓存不命中,重新上传提交。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)
    work = tmp_path / "work"
    _interrupted_run(pdf, work)

    # 换成一个内容不同的 PDF(页数相同,指纹必然不同)
    make_paged_pdf(tmp_path / "书.pdf", pages=5)
    assert len(cloud.posts) == 1

    result = _adapter().convert(pdf, work)

    assert len(cloud.posts) == 2                      # 重新提交
    assert result.task_id != "batch-1"
    assert result.book_md.exists()


# ---------------------------------------------------------------- 失败与降级

def test_plain_failure_is_not_degraded(tmp_path, cloud) -> None:
    """非「解析失败」的错误直接抛:不要白渲染一遍纯图重试(重复消耗额度)。"""
    cloud.fail_batches, cloud.fail_msg = 1, "rate limit exceeded"
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=4)

    with pytest.raises(MinerUError, match="rate limit exceeded"):
        _adapter().convert(pdf, tmp_path / "work")

    assert len(cloud.posts) == 1
    assert len(cloud.uploads) == 1


def test_parse_failure_degrades_to_rendered_pdf_and_retries(tmp_path, cloud) -> None:
    """只有 err_msg 真的是解析失败才降级:渲染纯图后重新提交,第二次成功。"""
    cloud.fail_batches = 1
    cloud.fail_msg = "parsing failed, please try again later"
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)
    work = tmp_path / "work"

    result = _adapter().convert(pdf, work)

    assert len(cloud.posts) == 2                      # 原文件 + 渲染纯图各一次
    assert len(cloud.uploads) == 2
    assert result.book_md.exists()
    assert not (work / "_rendered.pdf").exists()      # 临时渲染文件收尾清理
    assert "_rendered" in cloud.posts[1]["payload"]["files"][0]["name"]


# ---------------------------------------------------------------- hybrid 多区段各自续跑

class FlakyOCR:
    """假 OCR:提交即落盘 `.ocr_task.json`(像真后端那样),指定页会「中断」。"""

    name = "mineru"

    def __init__(self, fail_pages: set[int] | None = None) -> None:
        self.fail_pages = fail_pages or set()
        self.calls: list[int] = []
        self.dirs: list[str] = []            # 每次收到的区段工作目录名(用于断言路径稳定)

    def convert(self, pdf_path, work_dir) -> ConversionResult:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        page_no = int(re.search(r"p(\d+)", Path(pdf_path).stem).group(1))
        self.calls.append(page_no)
        self.dirs.append(work_dir.name)
        (work_dir / ".ocr_task.json").write_text(
            json.dumps({"backend": self.name, "variant": "original",
                        "batch_id": f"batch-p{page_no}"}, ensure_ascii=False),
            encoding="utf-8",
        )
        if page_no in self.fail_pages:
            raise RuntimeError("云端任务中断(模拟)")
        images = work_dir / "images"
        images.mkdir(parents=True, exist_ok=True)
        write_png(images / "fig.png")
        md = f"# OCR 段 p{page_no}\n\nOCR 正文段落,以句号结尾。\n"
        (work_dir / "book.md").write_text(md, encoding="utf-8")
        return ConversionResult(book_md=work_dir / "book.md", images_dir=images, backend=self.name)


def _hybrid_convert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ocr: FlakyOCR):
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTT")
    real = convert.get_backend

    def fake_get_backend(name: str, cfg: dict):
        return real("pymupdf", cfg) if name == "pymupdf" else ocr

    monkeypatch.setattr(convert, "get_backend", fake_get_backend)
    cfg = {"ocr_backend": "mineru", "pymupdf": {"write_images": True, "bold_fonts": []}}
    return convert.convert_auto(pdf, tmp_path / "work", config=cfg)[0]


def test_hybrid_failed_run_keeps_its_own_resume_cache(tmp_path, monkeypatch) -> None:
    """hybrid 每个区段一个工作目录:某段中断 → 该段目录与缓存必须留下(可续跑)。"""
    ocr = FlakyOCR(fail_pages={3})                    # 第 3 页起的那一段失败
    work = tmp_path / "work"

    with pytest.raises(RuntimeError, match="云端任务中断"):
        _hybrid_convert(tmp_path, monkeypatch, ocr)

    assert (work / "_scan_p3" / ".ocr_task.json").exists()   # 缓存保留
    assert not (work / "_scan_p6").exists()                  # 后续区段还没开始
    assert not list(work.glob("_hybrid_scan_*.pdf"))         # 临时纯图 PDF 已清理


def test_hybrid_success_cleans_every_run_dir(tmp_path, monkeypatch) -> None:
    ocr = FlakyOCR()
    result = _hybrid_convert(tmp_path, monkeypatch, ocr)
    work = result.book_md.parent

    assert ocr.calls == [3, 6]
    assert not list(work.glob("_scan_p*"))                   # 全部收尾清理
    assert not list(work.glob("_hybrid_scan_*.pdf"))
    assert page_marks(result.book_md.read_text(encoding="utf-8")) == [[1], [2], [3], [4], [5], [6], [7], [8]]


def test_hybrid_resume_reuses_the_same_work_dir(tmp_path, monkeypatch) -> None:
    """续跑时区段工作目录路径必须一致(否则缓存永远命中不了)。"""
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTT")
    work = tmp_path / "work"
    ocr = FlakyOCR(fail_pages={3})
    real = convert.get_backend
    monkeypatch.setattr(convert, "get_backend",
                        lambda name, cfg: real("pymupdf", cfg) if name == "pymupdf" else ocr)
    cfg = {"ocr_backend": "mineru", "pymupdf": {"write_images": True, "bold_fonts": []}}

    with pytest.raises(RuntimeError):
        convert.convert_auto(pdf, work, config=cfg)
    assert ocr.dirs == ["_scan_p3"]                   # 失败即止,缓存留在该目录
    assert (work / "_scan_p3" / ".ocr_task.json").exists()

    ocr.fail_pages = set()                            # 云端恢复,续跑
    convert.convert_auto(pdf, work, config=cfg)

    assert ocr.dirs == ["_scan_p3", "_scan_p3", "_scan_p6"]
    assert ocr.dirs[0] == ocr.dirs[1]                 # 同一区段续跑 → 同一目录(缓存命中)
    assert not list(work.glob("_scan_p*"))            # 成功后清理
