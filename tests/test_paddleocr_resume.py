"""PaddleOCR-VL 云端后端:续跑 / 复用 / 重试边界(全程离线,不发真实请求)。

与 MinerU 那套测试同样的思路:用**假 HTTP 层**替换 `paddleocr_backend.requests`,
被测的是 `PaddleOCRAdapter` 的完整流程(提交 → 轮询 → 下载 JSONL → 落盘),
所以「重跑到底有没有重新提交」这种计费问题按真实代码路径验证,而不是靠读代码判断。

PaddleOCR 一次提交整本(单 Token、单 job),因此它只有一层段缓存(`_parts/whole/`)
加一层任务缓存(`.ocr_task.json`)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backends import paddleocr_backend
from backends.base import is_retryable
from backends.paddleocr_backend import JOBS_URL, PaddleOCRAdapter, PaddleOCRError
from conftest import make_pdf

JSONL_URL = "https://result.local/out.jsonl"


class FakeResp:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None,
                 text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakePaddle:
    """假 PaddleOCR:记录提交/轮询/下载次数,状态按脚本给出。"""

    def __init__(self, poll_states: list[str] | None = None,
                 jsonl: str | None = None) -> None:
        self.posts: list[dict] = []
        self.polls = 0
        self.downloads = 0
        self.poll_states = list(poll_states or [])
        self.jsonl = jsonl if jsonl is not None else self._default_jsonl()

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
            self.polls += 1
            state = self._next_state()
            payload = {"code": 0, "data": {"jobId": url.rsplit("/", 1)[-1], "state": state}}
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
