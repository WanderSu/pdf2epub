"""PaddleOCR-VL 云端后端:续跑 / 复用 / 重试边界(全程离线,不发真实请求)。

与 MinerU 那套测试同样的思路:用**假 HTTP 层**替换 `paddleocr_backend.requests`,
被测的是 `PaddleOCRAdapter` 的完整流程(提交 → 轮询 → 下载 JSONL → 落盘),
所以「重跑到底有没有重新提交」这种计费问题按真实代码路径验证,而不是靠读代码判断。

PaddleOCR 一次提交整本(单 Token、单 job),因此它只有一层段缓存(`_parts/whole/`)
加一层任务缓存(`.ocr_task.json`)。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import pdf2epub.events as events
from pdf2epub.backends import paddleocr_backend
from pdf2epub.backends.base import is_retryable
from pdf2epub.backends.paddleocr_backend import (
    JOBS_URL,
    PaddleOCRAdapter,
    PaddleOCRError,
    extract_progress,
)
from conftest import make_pdf

JSONL_URL = "https://result.local/out.jsonl"


class FakeResp:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)
        # requests.Response 的流式接口:下载层会读 headers / iter_content,
        # 并把它当上下文管理器用(真实响应支持)
        self.headers: dict[str, str] = {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1 << 16):
        data = self.text.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def __enter__(self) -> "FakeResp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakePaddle:
    """假 PaddleOCR:记录提交/轮询/下载次数,状态按脚本给出。"""

    def __init__(self, poll_states: list[str] | None = None,
                 jsonl: str | None = None,
                 poll_progress: list[dict] | None = None) -> None:
        self.posts: list[dict] = []
        self.polls = 0
        self.downloads = 0
        self.poll_states = list(poll_states or [])
        self.jsonl = jsonl if jsonl is not None else self._default_jsonl()
        #: 每个状态依次附带的 `extractProgress`(不足时沿用最后一个,模拟真实云端)
        self.poll_progress = list(poll_progress or [])

    def _next_state(self) -> str:
        """按脚本给状态:列表里只剩一个时**一直**返回它(便于演「永远解析中」)。"""
        if len(self.poll_states) > 1:
            return self.poll_states.pop(0)
        return self.poll_states[0] if self.poll_states else "done"

    @staticmethod
    def _default_jsonl() -> str:
        page = {"result": {"layoutParsingResults": [
            {"markdown": {"text": "# 第一页\n\n识别出来的正文,以句号结尾。", "images": {}}}
        ]}}
        return json.dumps(page, ensure_ascii=False) + "\n"

    def counts(self) -> dict:
        return {"posts": len(self.posts), "polls": self.polls, "downloads": self.downloads}

    # --- 假 HTTP 接口(与 requests 同名同调用形态) ---
    def post(self, url: str, headers: dict | None = None, data: dict | None = None,
             files: dict | None = None, timeout: int | None = None) -> FakeResp:
        assert url == JOBS_URL, url
        self.posts.append({"data": data, "files": sorted((files or {}).keys())})
        return FakeResp(payload={"code": 0, "data": {"jobId": f"job-{len(self.posts)}"}})

    def get(self, url: str, headers: dict | None = None, timeout: int | None = None,
            stream: bool = False) -> FakeResp:
        if url.startswith(f"{JOBS_URL}/"):
            index = self.polls
            self.polls += 1
            state = self._next_state()
            payload = {"code": 0, "data": {"jobId": url.rsplit("/", 1)[-1], "state": state}}
            if self.poll_progress:
                # 第 N 次轮询带第 N 条进度;用完后沿用最后一条(与真实云端同形)
                payload["data"]["extractProgress"] = self.poll_progress[
                    min(index, len(self.poll_progress) - 1)]
            if state == "done":
                payload["data"]["resultUrl"] = {"jsonUrl": JSONL_URL}
            elif state == "failed":
                payload["data"]["errorMsg"] = "page too large"
            return FakeResp(payload=payload)
        if url == JSONL_URL:
            self.downloads += 1
            return FakeResp(text=self.jsonl)
        raise AssertionError(f"未预期的下载请求: {url}")


def _adapter(**kwargs) -> PaddleOCRAdapter:
    return PaddleOCRAdapter(token="fake-token", poll_interval=0, **kwargs)


@pytest.fixture()
def fake(monkeypatch) -> FakePaddle:
    fake = FakePaddle()
    monkeypatch.setattr(paddleocr_backend, "requests", fake)
    return fake


def test_completed_result_is_reused_without_any_request(tmp_path, fake) -> None:
    """成功一次之后再跑(清理/EPUB 失败重试、换 CSS 重转)不再碰云端一个字。"""
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"
    first = _adapter().convert(pdf, work)
    assert fake.counts() == {"posts": 1, "polls": 1, "downloads": 1}

    second = _adapter().convert(pdf, work)

    assert fake.counts() == {"posts": 1, "polls": 1, "downloads": 1}   # 连轮询都没有
    assert second.book_md.read_text(encoding="utf-8") == first.book_md.read_text(encoding="utf-8")
    assert not (work / ".ocr_task.json").exists()      # 任务缓存已清,段缓存接手


def test_interrupted_job_is_polled_again_not_resubmitted(tmp_path, monkeypatch) -> None:
    """提交后轮询超时(进程被杀/断网)→ 重跑继续轮询同一个 jobId,不重新上传。"""
    fake = FakePaddle(poll_states=["running", "running", "running"])
    monkeypatch.setattr(paddleocr_backend, "requests", fake)
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"

    with pytest.raises(PaddleOCRError, match="轮询超时"):
        _adapter(timeout=0.01).convert(pdf, work)

    assert len(fake.posts) == 1
    cached = json.loads((work / ".ocr_task.json").read_text(encoding="utf-8"))
    assert cached["job_id"] == "job-1"
    assert not (work / "_parts").exists()              # 还没拿到结果,没有段缓存

    fake.poll_states = ["done"]                        # 云端出结果了
    result = _adapter().convert(pdf, work)

    assert len(fake.posts) == 1                        # 没有第二次提交/上传
    assert result.task_id == "job-1"
    assert "第一页" in result.book_md.read_text(encoding="utf-8")


def test_dead_job_falls_back_to_a_new_submission(tmp_path, monkeypatch) -> None:
    """缓存的 job 已失效(探测拿不到)→ 只能重新提交,并且要真的提交。"""
    fake = FakePaddle(poll_states=["running"])      # 一直解析中 → 轮询超时
    monkeypatch.setattr(paddleocr_backend, "requests", fake)
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"

    with pytest.raises(PaddleOCRError, match="轮询超时"):
        _adapter(timeout=0.01).convert(pdf, work)

    # 探测返回 404:job-1 已失效(不是网络故障);其余请求照常走假云端
    real_get = fake.get

    def get_404(url: str, **kwargs):
        if url.endswith("/job-1"):
            return FakeResp(status_code=404, payload={"code": 404, "msg": "not found"})
        return real_get(url, **kwargs)

    monkeypatch.setattr(fake, "get", get_404)
    fake.poll_states = []                          # 新任务直接完成
    result = _adapter().convert(pdf, work)

    assert len(fake.posts) == 2                        # 重新提交了
    assert result.task_id == "job-2"


def test_failed_cloud_job_is_not_auto_retried(tmp_path, fake) -> None:
    """云端把整本判死 → 不自动重试(重试 = 整本重新上传、重新计费)。"""
    fake.poll_states = ["failed"]
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)

    with pytest.raises(PaddleOCRError, match="page too large") as excinfo:
        _adapter().convert(pdf, tmp_path / "work")

    assert is_retryable(excinfo.value) is False


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, False),
                                              (400, False), (500, True)])
def test_submit_http_status_decides_retryability(status: int, retryable: bool,
                                                 tmp_path, monkeypatch) -> None:
    """提交阶段的 HTTP 错误同样要分:认证/配额类不重试,5xx 才重试。"""
    fake = FakePaddle()
    monkeypatch.setattr(paddleocr_backend, "requests", fake)
    monkeypatch.setattr(fake, "post", lambda *a, **k: FakeResp(
        status_code=status, payload={"msg": "boom"}))

    with pytest.raises(PaddleOCRError) as excinfo:
        _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=1), tmp_path / "work")

    assert is_retryable(excinfo.value) is retryable


def test_missing_token_is_not_retryable(monkeypatch) -> None:
    """没有 Token 是配置问题:重试多少次都一样,批处理不该再花时间重跑。"""
    monkeypatch.delenv("PADDLEOCR_TOKEN", raising=False)
    monkeypatch.setattr(paddleocr_backend, "load_api_key", lambda *_a, **_k: "")

    with pytest.raises(PaddleOCRError, match="Token") as excinfo:
        PaddleOCRAdapter()

    assert is_retryable(excinfo.value) is False


def test_changed_params_invalidate_the_part_cache(tmp_path, fake) -> None:
    """OCR 开关变了(如版面方向校正)→ 旧结果不能复用。"""
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)

    _adapter(use_doc_unwarping=True).convert(pdf, work)

    assert len(fake.posts) == 2


# ---------------------------------------------------------------- 脚注标签过滤

def _optional_payload(fake: FakePaddle) -> dict:
    return json.loads(fake.posts[0]["data"]["optionalPayload"])


def test_submit_keeps_footnotes_by_default(tmp_path, fake) -> None:
    """云端默认会忽略 footnote → 必须显式传 markdownIgnoreLabels 把脚注留下来。

    传的是「官方默认值 − footnote」:页眉页脚/页码/边注仍被过滤(否则噪声全进正文),
    只有脚注不再被丢。
    """
    _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=2), tmp_path / "work")

    labels = _optional_payload(fake).get("markdownIgnoreLabels")
    assert labels is not None, "没有传 markdownIgnoreLabels 时云端会按默认值丢掉脚注"
    assert "footnote" not in labels
    assert {"header", "footer", "number", "aside_text"} <= set(labels)


def test_submit_honours_configured_labels(tmp_path, fake) -> None:
    """配置里给的过滤列表原样上送(含显式 [] = 什么都不过滤)。"""
    _adapter(markdown_ignore_labels=[]).convert(
        make_pdf(tmp_path / "扫描书.pdf", pages=2), tmp_path / "work")

    assert _optional_payload(fake)["markdownIgnoreLabels"] == []


def test_changed_labels_invalidate_the_part_cache(tmp_path, fake) -> None:
    """「忽略 footnote」与「保留 footnote」是两次不同的 OCR,不能共用段缓存。"""
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"
    _adapter().convert(pdf, work)

    _adapter(markdown_ignore_labels=["footnote"]).convert(pdf, work)

    assert len(fake.posts) == 2

    # 标签一致时仍然复用(不因指纹里多了个字段就次次重跑)
    _adapter(markdown_ignore_labels=["footnote"]).convert(pdf, work)
    assert len(fake.posts) == 2


# ---------------------------------------------------------------- 空结果守卫

def test_unparsable_result_raises_and_is_not_cached(tmp_path, monkeypatch) -> None:
    """结果地址返回 HTML 错误页:必须抛错,不能把空 book.md 当成功缓存。

    修复前每一行解析失败就 continue,空 md_text 照写 book.md 并落段缓存 ——
    之后每次重跑都命中这份「已完成」的空缓存,静默产出一本空书,网络恢复也救不回来。
    """
    fake = FakePaddle(jsonl="<html><body>503 Service Unavailable</body></html>")
    monkeypatch.setattr(paddleocr_backend, "requests", fake)
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"

    with pytest.raises(PaddleOCRError, match="无法解析") as err:
        _adapter().convert(pdf, work)

    assert is_retryable(err.value)                 # 网络/代理问题:可重试
    assert not (work / "_parts").exists()          # 空结果没有落段缓存
    assert (work / ".ocr_task.json").exists()      # jobId 留着,重跑续取同一个任务

    # 同一个任务后来拿到正常结果:直接出书,不重新提交
    fake.jsonl = FakePaddle._default_jsonl()
    result = _adapter().convert(pdf, work)
    assert len(fake.posts) == 1
    assert "第一页" in result.book_md.read_text(encoding="utf-8")


def test_result_with_zero_pages_is_an_error(tmp_path, monkeypatch) -> None:
    """JSONL 合法但 0 页内容(空数组)同样不能当成功缓存。"""
    fake = FakePaddle(jsonl='{"result": {"layoutParsingResults": []}}\n')
    monkeypatch.setattr(paddleocr_backend, "requests", fake)
    pdf = make_pdf(tmp_path / "扫描书.pdf", pages=2)
    work = tmp_path / "work"

    with pytest.raises(PaddleOCRError, match="为空"):
        _adapter().convert(pdf, work)
    assert not (work / "_parts").exists()


# ---------------------------------------------------------------- 页级进度事件
#
# 被钉住的缺陷:轮询只把「解析中 (3/268 页)」打进中文日志,从不发 `progress` 事件 ——
# 桌面端进度条只认事件的 current/total,于是整段解析期间一直显示 0/总页数。


@pytest.fixture()
def event_buffer():
    """开启事件流并把解析出来的事件交回用例(结束时关闭,避免污染别的用例)。"""
    buf = io.StringIO()
    events.configure(stream=buf)
    try:
        yield buf
    finally:
        events.configure(False)


def _read_events(buf: io.StringIO, name: str = "progress") -> list[dict]:
    return [obj for obj in (json.loads(line) for line in buf.getvalue().splitlines() if line.strip())
            if obj["event"] == name]


def test_poll_reports_page_progress_as_events(tmp_path, monkeypatch, event_buffer) -> None:
    """解析中/排队中都要把云端页数报给事件流,桌面端才能显示「3/268 页」。"""
    fake = FakePaddle(
        poll_states=["pending", "running", "running", "done"],
        poll_progress=[{}, {"totalPages": 268, "extractedPages": 0},
                       {"totalPages": 268, "extractedPages": 3},
                       {"totalPages": 268, "extractedPages": 268}],
    )
    monkeypatch.setattr(paddleocr_backend, "requests", fake)

    _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=2), tmp_path / "work")

    progress = _read_events(event_buffer)
    assert [(e["current"], e["total"]) for e in progress] == [(0, 268), (3, 268)]
    assert all(e["stage"] == "extract" and e["detail"] == "paddleocr" for e in progress)


def test_malformed_progress_does_not_break_polling(tmp_path, monkeypatch, event_buffer) -> None:
    """云端少给/给错进度字段只是少一次进度上报,绝不能让轮询中断。"""
    fake = FakePaddle(
        poll_states=["running", "running", "done"],
        poll_progress=[{"totalPages": None, "extractedPages": 5},
                       {"totalPages": "abc"},
                       {}],
    )
    monkeypatch.setattr(paddleocr_backend, "requests", fake)

    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=2), tmp_path / "work")

    assert _read_events(event_buffer) == []        # 没有可用页数就不上报
    assert "第一页" in result.book_md.read_text(encoding="utf-8")


@pytest.mark.parametrize("data,expected", [
    ({"extractProgress": {"totalPages": 10, "extractedPages": 4}}, (4, 10)),
    ({"extractProgress": {"totalPages": "10", "extractedPages": "4"}}, (4, 10)),
    ({"extractProgress": {"totalPages": 10, "extractedPages": 99}}, (10, 10)),   # 夹到总数内
    ({"extractProgress": {"totalPages": 10, "extractedPages": -3}}, (0, 10)),
    ({"extractProgress": {"totalPages": 0, "extractedPages": 0}}, None),
    ({"extractProgress": None}, None),
    ({"extractProgress": "oops"}, None),
    ({}, None),
])
def test_extract_progress_parsing(data, expected) -> None:
    assert extract_progress(data) == expected


# ---------------------------------------------------------------- 页级页码注释
#
# 被钉住的缺陷:PaddleOCR 合并出来的 Markdown **没有页码注释**,于是内容对照的
# 「页覆盖」检查整条空转(`content_covered_pages=0`,既不是 0% 也不是 100%,是
# 「没法算」)。MinerU 分片有页注释,PaddleOCR 这一路也必须写,空页同样写 ——
# 否则「这页没识别出文字」会被算成「这页不存在」。


def test_every_page_gets_a_page_marker(tmp_path, monkeypatch) -> None:
    pages = [
        {"markdown": {"text": "# 第一页\n\n正文一,句号结尾。", "images": {}}},
        {"markdown": {"text": "", "images": {}}},            # 空白页(OCR 没识别出文字)
        {"markdown": {"text": "第三页正文,内容足够长。", "images": {}}},
    ]
    jsonl = json.dumps({"result": {"layoutParsingResults": pages}}, ensure_ascii=False) + "\n"
    monkeypatch.setattr(paddleocr_backend, "requests", FakePaddle(jsonl=jsonl))

    result = _adapter().convert(make_pdf(tmp_path / "扫描书.pdf", pages=3),
                                tmp_path / "work")
    md = result.book_md.read_text(encoding="utf-8")

    from pdf2epub.epub.content import covered_pages
    from pdf2epub.page_result import page_marks

    assert page_marks(md) == [[1], [2], [3]]
    assert covered_pages(md) == 3
    assert "<!-- page 2 -->" in md, "空白页也要有页注释,否则页覆盖会少算一页"

