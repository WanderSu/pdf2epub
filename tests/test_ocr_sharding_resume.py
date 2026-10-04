"""MinerU 分片与续跑集成测试(v0.3.2 P0-2 / v0.4.2 段级结果缓存)。

目标:把「>200 页自动分片」「段序合并」「中断后复用已提交任务」「已完成段不重复 OCR」
「只补失败/缺失的段」「--no-resume」这些**只有真实云端才会暴露**的行为,搬到离线、
可重复的测试里。

做法:用假云端替换 `mineru_backend.requests` —— 一个记录调用并吐出预设状态的
假 HTTP 层(提交/上传/轮询/下载全走它,包括真的解一个真 zip),所以被测的是
`MinerUAdapter` 的完整流程,而不是被 mock 掉的内部函数。假云端有**条目级**旋钮
(`stuck` / `fail_items`),可以演「一部分段完成、某段卡住或判死,之后云端恢复」,
这是验证「重跑不重复计费」的关键。

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
from backends.base import (ConversionResult, PartStore, TaskCache, file_fingerprint,
                           is_retryable)
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
    - `fail_batch_ids`:指定 batch id 全 failed(用于「第 2 段失败」这类脚本)
    - `fail_items` / `fail_msg_items`:指定**条目**(data_id)失败及其错误信息
    - `stuck`:指定条目永远 running(模拟云端迟迟出不来结果 → 轮询超时)
    - `fail_downloads`:前 N 次结果下载返回 500(模拟「云端有结果,我们没接住」)
    - `fail_msg`:失败时返回的 err_msg
    - `stale`:所有轮询都返回空列表(模拟缓存里的 batch 已失效/无权限)

    条目级的旋钮(stuck/fail_items)用**可变的 set**,测试可以中途清空它们来表示
    「云端恢复了」,从而验证重跑只补缺失的段、不重复买已完成的段。
    """

    def __init__(self, *, pending_polls: int = 0, fail_batches: int = 0,
                 fail_msg: str = "random failure", stale: bool = False) -> None:
        self.pending_polls = pending_polls
        self.fail_batches = fail_batches
        self.fail_msg = fail_msg
        self.stale = stale
        self.stuck: set[str] = set()
        self.fail_items: set[str] = set()
        self.fail_batch_ids: set[str] = set()
        self.fail_msg_items: dict[str, str] = {}
        self.fail_downloads = 0
        self.batches: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.uploads: list[str] = []
        self.polls = 0
        #: data_id → 页脚脚注文本(有值时结果包里会带上 content_list.json)
        self.footnotes: dict[str, str] = {}

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
            if self.fail_downloads > 0:              # 云端有结果,但这次没接住
                self.fail_downloads -= 1
                return FakeResponse(status_code=500)
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
        failing = (int(batch_id.split("-")[1]) <= self.fail_batches
                   or batch_id in self.fail_batch_ids)
        items = []
        for entry in batch["entries"]:
            key = entry.get("data_id") or entry["name"]
            item = {
                "data_id": entry.get("data_id") or entry["name"],
                "file_name": entry["name"],
                "state": "running" if pending else "done",
            }
            if key in self.stuck:
                item["state"] = "running"        # 永远不出结果(触发轮询超时)
            elif not pending:
                if key in self.fail_items or failing:
                    item["state"] = "failed"
                    item["err_msg"] = self.fail_msg_items.get(key, self.fail_msg)
                else:
                    item["full_zip_url"] = f"https://download.local/{batch_id}/{item['data_id']}"
            items.append(item)
        return FakeResponse(payload={"code": 0, "data": {"extract_result": items}})

    def _zip_bytes(self, data_id: str) -> bytes:
        """每个分段固定产出:full.md + images/fig.png,用于测跨段重名处理。

        图片字节带上段标识 → 不同段里的同名图是**内容不同的两张图**,合并时必须
        改名(加段前缀)并同步改引用,否则后一段的图会盖掉前一段的。

        `footnotes[data_id]` 有值时额外产出结构化结果 `*_content_list.json`:
        **full.md 里没有页脚脚注,脚注只在结构化结果里** —— 这是真实 MinerU 的行为
        (page_footnote 属 discarded blocks),用来验证后端的补回逻辑。
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"{data_id}/full.md",
                        f"# 分段 {data_id}\n\n这是 {data_id} 的正文段落,以句号结尾。\n\n"
                        f"![图](images/fig.png)\n")
            zf.writestr(f"{data_id}/images/fig.png", f"\x89PNG-fake-{data_id}".encode())
            note = self.footnotes.get(data_id)
            if note:
                blocks = [
                    {"type": "text", "text": f"这是 {data_id} 的正文段落,以句号结尾。",
                     "page_idx": 0},
                    {"type": "page_footnote", "text": note, "page_idx": 0},
                    {"type": "page_number", "text": "1", "page_idx": 0},
                ]
                zf.writestr(f"{data_id}/{data_id}_content_list.json",
                            json.dumps(blocks, ensure_ascii=False))
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


def install_fake_cloud(monkeypatch: pytest.MonkeyPatch) -> FakeMinerUCloud:
    """把 `requests` / `time.sleep` 换成假 MinerU:夹具与别处复用同一个安装函数。

    (直接调用夹具函数已被 pytest 判为错误用法,所以安装逻辑放在普通函数里。)
    """
    monkeypatch.setattr(mineru_backend.time, "sleep", lambda *_: None)
    fake = FakeMinerUCloud()
    monkeypatch.setattr(mineru_backend, "requests", _RequestsShim(fake))
    return fake


@pytest.fixture
def cloud(monkeypatch: pytest.MonkeyPatch) -> FakeMinerUCloud:
    """假云端(**单个实例**跨多次 convert 复用 —— 续跑用例要看到上一次的提交记录)。

    需要「排队中 / 解析失败 / 缓存失效」等脚本时,直接改实例属性:
    `cloud.pending_polls = 2`、`cloud.fail_batches = 1`、`cloud.stale = True`。
    """
    return install_fake_cloud(monkeypatch)


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


# ---------------------------------------------------------------- 页脚脚注补回

def test_page_footnote_from_structured_result_is_recovered(tmp_path, cloud) -> None:
    """`full.md` 不含 page_footnote(真实 MinerU 行为)→ 合并时从结构化结果补回。

    补回位置是**该页最后一块内容之后**,而不是丢到文末;页码之类的 discarded blocks
    不补(那是页面装饰,markdown 侧本来就要过滤掉)。
    """
    cloud.footnotes["书.pdf"] = "① 这是测试脚注:内容不能丢。"
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)

    result = _adapter().convert(pdf, tmp_path / "work")

    md = result.book_md.read_text(encoding="utf-8")
    assert "这是测试脚注" in md
    assert md.index("正文段落,以句号结尾。") < md.index("这是测试脚注")
    assert result.stats["recovered_footnotes"] == 1
    assert "\n1\n" not in md                       # page_number 不被补进正文
    # 结构化结果随段缓存落盘:否则下次续跑合并时补不回来
    assert list((tmp_path / "work" / "_parts").rglob("content_list.json"))

    # 新进程只看得到磁盘:再跑一次(命中段缓存、不重新提交云端任务)脚注依然补得回
    again = _adapter().convert(pdf, tmp_path / "work")

    assert len(cloud.posts) == 1
    assert "这是测试脚注" in again.book_md.read_text(encoding="utf-8")
    assert again.stats["recovered_footnotes"] == 1


def test_cached_part_without_structured_result_still_merges(tmp_path, cloud) -> None:
    """本次改动之前落的段缓存没有 content_list.json:照常复用合并,只是补不回脚注。

    不能因为缺一个附带文件就把整段判成失效 —— 那会让用户为一个附带文件重新
    上传、重新 OCR、重复计费。
    """
    cloud.footnotes["书.pdf"] = "① 旧结果里的脚注。"
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)
    for path in (work / "_parts").rglob("content_list.json"):
        path.unlink()

    result = _adapter().convert(pdf, work)

    assert len(cloud.posts) == 1                   # 没有重新提交云端任务
    assert "旧结果里的脚注" not in result.book_md.read_text(encoding="utf-8")


def test_failed_extra_file_write_leaves_no_usable_cache(tmp_path, monkeypatch) -> None:
    """附带文件写失败时 meta 还没写 → 这段不算命中,绝不留下「有 meta 却没内容」的假缓存。

    代价是这一段要重新 OCR;反过来(先写 meta 再写文件)就会出现「缓存命中但脚注
    永久缺失」的错误状态,比多花一次额度更糟。
    """
    import shutil

    store = PartStore(tmp_path / "work", "mineru", "源指纹", resume=True)
    src = tmp_path / "content_list.json"
    src.write_text("[]", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("磁盘满")

    monkeypatch.setattr(shutil, "copy2", boom)

    with pytest.raises(RuntimeError, match="磁盘满"):
        store.save("1-1", "params", "# 正文", extra_files={"content_list.json": src})

    assert not (store.dir_for("1-1") / ".part.json").exists()
    assert store.has_result("1-1", "params") is False


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


class FingerprintOCR:
    """假 OCR:像真后端那样按**收到的 PDF 内容指纹**复用云端任务。

    与 FlakyOCR 的差别是关键:FlakyOCR 只写一个不含指纹的 `.ocr_task.json`、从不
    load,所以「临时纯图 PDF 每次渲染字节不同 → 指纹对不上 → 缓存永不命中」这个
    真实缺陷在它身上测不出来。这里用真的 TaskCache。
    """

    name = "mineru"

    def __init__(self, fail_next: bool = True) -> None:
        self.fail_next = fail_next        # 只让第一次「新提交」中断一次
        self.submits = 0
        self.resumes = 0

    def convert(self, pdf_path, work_dir) -> ConversionResult:
        pdf_path, work_dir = Path(pdf_path), Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        cache = TaskCache(work_dir, self.name)
        hit = cache.load(pdf_path, "original")
        if hit:
            self.resumes += 1
        else:
            self.submits += 1
            cache.save(pdf_path, "original", batch_id=f"batch-{self.submits}")
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("云端任务中断(模拟)")
        images = work_dir / "images"
        images.mkdir(parents=True, exist_ok=True)
        write_png(images / "fig.png")
        md = f"# OCR 段 {work_dir.name}\n\nOCR 正文段落,以句号结尾。\n"
        (work_dir / "book.md").write_text(md, encoding="utf-8")
        return ConversionResult(book_md=work_dir / "book.md", images_dir=images, backend=self.name)


def test_hybrid_resume_hits_cache_with_real_fingerprint(tmp_path, monkeypatch) -> None:
    """hybrid 续跑要真的命中云端缓存:临时纯图 PDF 必须字节稳定。

    旧缺陷:PyMuPDF 每次 save 都写新的 /ID → 同一批页渲染两次字节不同 → 云端缓存的
    内容指纹永不相等 → 中断后重跑重新上传整段、重复扣额度(此断言会是 3/0)。
    """
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTT")
    work = tmp_path / "work"
    ocr = FingerprintOCR()
    real = convert.get_backend
    monkeypatch.setattr(convert, "get_backend",
                        lambda name, cfg: real("pymupdf", cfg) if name == "pymupdf" else ocr)
    cfg = {"ocr_backend": "mineru", "pymupdf": {"write_images": True, "bold_fonts": []}}

    with pytest.raises(RuntimeError, match="云端任务中断"):
        convert.convert_auto(pdf, work, config=cfg)
    assert (ocr.submits, ocr.resumes) == (1, 0)

    convert.convert_auto(pdf, work, config=cfg)       # 续跑

    # 第 3 页那一段复用已提交的任务(不重传),只有第 6 页是新提交
    assert (ocr.submits, ocr.resumes) == (2, 1)


# ---------------------------------------------------------------- 段级结果缓存(不重复计费)

def _part_dirs(work: Path) -> list[str]:
    """工作目录里已落盘的段(段标识即目录名)。"""
    parts = work / "_parts"
    return sorted(p.name for p in parts.iterdir()) if parts.is_dir() else []


def test_timeout_salvages_finished_parts_and_resumes_the_same_batch(tmp_path, cloud) -> None:
    """Case A:OCR 完成一部分后中断 —— 已完成段先落盘,重跑接着等同一个批次。

    8 页 / 每段 2 页 → 4 段;第 3 段卡住导致轮询超时。其余 3 段必须已经在磁盘上
    (它们已经计费了),重跑既不重新上传、也不重新 OCR。
    """
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=8)
    work = tmp_path / "work"
    cloud.stuck = {"part-3"}

    with pytest.raises(MinerUError, match="轮询超时"):
        _adapter(max_pages_per_task=2, timeout=1, poll_interval=0.05).convert(pdf, work)

    assert _part_dirs(work) == ["1-2", "3-4", "7-8"]          # 目录名就是页码范围
    assert (len(cloud.posts), len(cloud.uploads)) == (1, 4)
    cached = json.loads((work / ".ocr_task.json").read_text(encoding="utf-8"))
    assert cached["batch_id"] == "batch-1"        # 批次 id 留着,重跑接着等

    cloud.stuck.clear()                           # 云端恢复
    posts, uploads = len(cloud.posts), len(cloud.uploads)
    result = _adapter(max_pages_per_task=2).convert(pdf, work)

    assert (len(cloud.posts), len(cloud.uploads)) == (posts, uploads)   # 零提交、零上传
    assert result.task_id == "batch-1"
    md = result.book_md.read_text(encoding="utf-8")
    assert page_marks(md) == [[1, 2], [3, 4], [5, 6], [7, 8]]
    assert md.index("分段 part-1") < md.index("分段 part-2") < md.index("分段 part-3")


def test_failed_shard_is_retried_alone_and_finished_shards_are_reused(tmp_path, cloud) -> None:
    """Case C:某段云端判死 —— 已完成的段先抢救落盘,重跑只补那一段。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=8)
    work = tmp_path / "work"
    cloud.fail_items = {"part-2"}

    with pytest.raises(MinerUError, match="3-4 仍缺结果"):
        _adapter(max_pages_per_task=2).convert(pdf, work)

    assert _part_dirs(work) == ["1-2", "5-6", "7-8"]          # 失败段的目录不会骗人
    assert (len(cloud.posts), len(cloud.uploads)) == (1, 4)
    assert not (work / ".ocr_task.json").exists()   # 死批次不能永远卡住重跑

    cloud.fail_items.clear()                        # 云端恢复
    result = _adapter(max_pages_per_task=2).convert(pdf, work)

    assert len(cloud.posts) == 2                    # 只补提交失败的那一段
    files = cloud.posts[1]["payload"]["files"]
    assert [f["data_id"] for f in files] == ["part-2"]
    assert [f["page_ranges"] for f in files] == ["3-4"]   # 页码区间必须保留
    assert len(cloud.uploads) == 5                  # 只多上传 1 次
    md = result.book_md.read_text(encoding="utf-8")
    assert page_marks(md) == [[1, 2], [3, 4], [5, 6], [7, 8]]
    assert "分段 part-2" in md


def test_download_failure_keeps_the_batch_for_the_next_run(tmp_path, cloud) -> None:
    """结果下载失败(网络/磁盘)不许丢掉批次 id。

    这是「用户以为在恢复、其实又买了一次」最隐蔽的一条:云端早已把结果给出去了,
    只是我们没接住。批次 id 一丢,重跑就变成重新上传 + 重新 OCR + 重复计费。
    """
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=2)
    work = tmp_path / "work"
    cloud.fail_downloads = 1

    with pytest.raises(RuntimeError):            # 下载 500(requests.HTTPError 的替身)
        _adapter().convert(pdf, work)

    cached = json.loads((work / ".ocr_task.json").read_text(encoding="utf-8"))
    assert cached["batch_id"] == "batch-1"       # 批次还活着
    assert len(cloud.posts) == 1

    result = _adapter().convert(pdf, work)       # 重跑:继续取同一个批次的结果

    assert (len(cloud.posts), len(cloud.uploads)) == (1, 1)
    assert "分段 书.pdf" in result.book_md.read_text(encoding="utf-8")


def test_successful_run_is_fully_reused_on_rerun(tmp_path, cloud) -> None:
    """Case D:完全成功后再跑一次(EPUB 阶段失败后的重试/换 CSS 重转/--force)不碰云端。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)
    work = tmp_path / "work"
    first = _adapter().convert(pdf, work)
    md_first = first.book_md.read_text(encoding="utf-8")
    assert (len(cloud.posts), len(cloud.uploads)) == (1, 1)

    again = _adapter().convert(pdf, work)

    assert (len(cloud.posts), len(cloud.uploads)) == (1, 1)
    assert again.book_md.read_text(encoding="utf-8") == md_first
    # 复用同一张图,不产生第二份文件(重跑产物逐字节一致)
    assert [p.name for p in (work / "images").iterdir()] == ["fig.png"]


def test_resume_false_ignores_the_part_cache(tmp_path, cloud) -> None:
    """--no-resume:段结果缓存也要让路(强制重新提交是用户明示的)。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)

    result = _adapter(resume=False).convert(pdf, work)

    assert (len(cloud.posts), len(cloud.uploads)) == (2, 2)
    assert result.book_md.exists()


def test_changed_source_invalidates_the_part_cache(tmp_path, cloud) -> None:
    """源文件内容变了(指纹不同)→ 段缓存不命中,重新提交。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)
    make_paged_pdf(pdf, pages=4)                    # 同名文件,内容变了

    _adapter().convert(pdf, work)

    assert len(cloud.posts) == 2


def test_part_cache_key_includes_ocr_params(tmp_path, cloud) -> None:
    """OCR 参数变了(如 language)→ 旧结果不能复用。"""
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)

    _adapter(language="en").convert(pdf, work)

    assert len(cloud.posts) == 2


# ---------------------------------------------------------------- 渲染与指纹

def test_render_for_hybrid_is_byte_stable(tmp_path) -> None:
    """临时纯图 PDF 必须字节稳定 —— 它正是云端续跑缓存的指纹来源。

    这条用例守住 `out.save(..., no_new_id=True)`:PyMuPDF 默认每次保存都会写新的 /ID,
    同一批页渲染两次字节不同 → 指纹永不相等 → hybrid 中断后重跑会重新上传整段。
    """
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTT")
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"

    convert._render_pages_to_pdf(pdf, [2], a)
    convert._render_pages_to_pdf(pdf, [2], b)

    assert a.read_bytes() == b.read_bytes()
    assert file_fingerprint(a) == file_fingerprint(b)


def test_hybrid_render_stays_far_under_the_cloud_size_limit(tmp_path) -> None:
    """扫描区段的临时 PDF 必须远小于云端 200MB 上限。

    同样 20 页 150dpi:PNG 渲染实测 124MB(6.2MB/页 —— PyMuPDF 会把 PNG 位图原样
    存进 PDF),一段 30 页以上的扫描区段就会顶破上限、云端拒收;JPEG q85 只有几 MB。
    """
    pdf = make_paged_pdf(tmp_path / "扫描书.pdf", pages=20)
    out = tmp_path / "seg.pdf"
    convert._render_pages_to_pdf(pdf, list(range(20)), out)

    assert out.stat().st_size < 30 * 1024 * 1024


# ---------------------------------------------------------------- 重试边界

@pytest.mark.parametrize("status,retryable", [
    (400, False), (401, False), (403, False), (404, False), (429, False),
    (408, True), (500, True), (502, True),
])
def test_http_status_decides_whether_a_retry_is_worthwhile(status: int, retryable: bool) -> None:
    """认证/参数/配额类错误不重试(扫描书每重试一次就多扣一次额度);5xx/408 才重试。"""
    with pytest.raises(MinerUError) as excinfo:
        MinerUAdapter._parse(FakeResponse(status_code=status, payload={"msg": "boom"}))

    assert is_retryable(excinfo.value) is retryable


def test_cloud_shard_failure_is_not_auto_retried(tmp_path, cloud) -> None:
    """云端判死某段 → 不自动重试(重试就是再买一次同一本),但错误信息要说清下一步。"""
    cloud.fail_items = {"书.pdf"}                   # 整本一个任务 → 匹配键就是文件名
    cloud.fail_msg_items = {"书.pdf": "rate limit exceeded"}
    pdf = make_paged_pdf(tmp_path / "书.pdf", pages=3)

    with pytest.raises(MinerUError, match="rate limit exceeded") as excinfo:
        _adapter().convert(pdf, tmp_path / "work")

    assert is_retryable(excinfo.value) is False


# ---------------------------------------------------------------- hybrid 段间复用(真实后端)

def _hybrid_with_real_mineru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """hybrid 流程 + **真实 MinerUAdapter** + 假云端(只有 HTTP 层是假的)。"""
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTT")
    work = tmp_path / "work"
    adapter = MinerUAdapter(token="fake-token", poll_interval=0, timeout=30)
    real = convert.get_backend
    monkeypatch.setattr(convert, "get_backend",
                        lambda name, cfg: real("pymupdf", cfg) if name == "pymupdf" else adapter)
    cfg = {"ocr_backend": "mineru", "pymupdf": {"write_images": True, "bold_fonts": []}}
    return pdf, work, cfg


def test_hybrid_keeps_finished_segment_and_does_not_reocr_it(tmp_path, monkeypatch, cloud) -> None:
    """Case B:前面区段成功、后面区段失败 → 重跑只补失败的那一段。

    这是最贵的重复计费路径(整段扫描页重新上传 + 重新 OCR)。它同时依赖两件事:
    段结果落盘、以及临时纯图 PDF 字节稳定(字节漂移会让第二次运行多提交一次)。
    """
    pdf, work, cfg = _hybrid_with_real_mineru(tmp_path, monkeypatch)
    cloud.fail_batch_ids = {"batch-2"}              # 第 6 页起的那个区段失败

    with pytest.raises(MinerUError):
        convert.convert_auto(pdf, work, config=cfg)

    assert _part_dirs(work / "_scan_p3")            # 第 3 页那段的云端结果留在磁盘上
    assert not list(work.glob("_hybrid_scan_*.pdf"))     # 临时纯图 PDF 已清理
    assert (work / "_scan_p6").is_dir()
    assert not (work / "_scan_p6" / ".ocr_task.json").exists()   # 死批次不卡重跑
    posts, uploads = len(cloud.posts), len(cloud.uploads)
    assert (posts, uploads) == (2, 2)

    cloud.fail_batch_ids.clear()                    # 云端恢复
    result = convert.convert_auto(pdf, work, config=cfg)[0]

    assert len(cloud.posts) == posts + 1            # 只补第 6 页那一段
    assert len(cloud.uploads) == uploads + 1
    md = result.book_md.read_text(encoding="utf-8")
    assert page_marks(md) == [[1], [2], [3], [4], [5], [6], [7], [8]]
    assert "分段 _hybrid_scan_p3" in md and "分段 _hybrid_scan_p6" in md


def test_hybrid_rerun_after_success_touches_no_cloud(tmp_path, monkeypatch, cloud) -> None:
    """hybrid 成功后再跑(换 CSS 重转 / --force)→ 两个区段都直接复用,零云端调用。"""
    pdf, work, cfg = _hybrid_with_real_mineru(tmp_path, monkeypatch)
    first = convert.convert_auto(pdf, work, config=cfg)[0]
    assert (len(cloud.posts), len(cloud.uploads)) == (2, 2)

    again = convert.convert_auto(pdf, work, config=cfg)[0]

    assert (len(cloud.posts), len(cloud.uploads)) == (2, 2)     # 没有新提交、没有新上传
    assert (page_marks(again.book_md.read_text(encoding="utf-8"))
            == page_marks(first.book_md.read_text(encoding="utf-8")))
