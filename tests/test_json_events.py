"""`--json-events` 事件流回归(v0.4.0 P0-1)。

被钉住的旧缺陷:桌面端用 5 个正则从 CLI 的**中文日志**里猜状态 —— 文案一改就
静默失效(进度百分比只能按「日志行到达 +5」估算)。这里把「事件流是唯一可信
状态源」定死:

- 开启后 **stdout 只允许出现 JSON Lines**(人类日志改道 stderr),混进任何
  非 JSON 行都会让用例直接失败;
- `hello` 带的阶段权重之和必须是 1.0(前端按它算真实进度,不两边各写一份);
- hybrid 的 `ocr_runs` 与 MinerU 的 `shards`/`progress` 都来自**真实规划**,
  不是日志文案;
- 未开启时行为与从前完全一致(日志仍在 stdout)。

全程离线:本地样本走 PyMuPDF,云端路径用假后端 / 假 MinerU 服务。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import events
from cli import main as cli_main
from conftest import make_paged_pdf, make_pdf

pytestmark = pytest.mark.usefixtures("_reset_events")


@pytest.fixture(autouse=True)
def _reset_events():
    """每个用例后关掉事件流:它是模块级全局状态,串到别的用例会污染 stdout。"""
    yield
    events.configure(False)


def parse_json_lines(text: str) -> list[dict]:
    """逐行解析事件流:任何非 JSON 行都是对 stdout 的污染,直接失败。"""
    items: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        assert isinstance(obj, dict) and "event" in obj, f"不是事件行: {line!r}"
        items.append(obj)
    return items


def events_of(items: list[dict], name: str) -> list[dict]:
    return [i for i in items if i["event"] == name]


# ---------------------------------------------------------------- 事件模块本体

def test_sink_writes_json_lines_with_hello_and_weights() -> None:
    buf = io.StringIO()
    events.configure(stream=buf)
    events.emit("detect", type="text", pages=3)
    events.stage("clean", "start")
    events.progress("extract", 1, 4, detail="mineru")

    items = parse_json_lines(buf.getvalue())
    assert items[0]["event"] == "hello"
    weights = {s["name"]: s["weight"] for s in items[0]["stages"]}
    assert abs(sum(weights.values()) - 1.0) < 1e-9, "阶段权重之和必须是 1.0"
    assert [i["event"] for i in items[1:]] == ["detect", "stage", "progress"]
    assert items[1]["pages"] == 3
    assert (items[2]["name"], items[2]["state"]) == ("clean", "start")
    assert (items[3]["stage"], items[3]["current"], items[3]["total"]) == ("extract", 1, 4)


def test_emit_is_noop_when_disabled() -> None:
    assert not events.enabled()
    events.emit("detect", pages=1)          # 未开启:不抛异常、不输出
    events.stage("clean", "start")
    assert not events.enabled()


def test_progress_with_zero_total_is_skipped() -> None:
    """total<=0 时不发进度:前端不必再写除零保护。"""
    buf = io.StringIO()
    events.configure(stream=buf)
    events.progress("extract", 0, 0)
    events.progress("extract", 1, -1)
    assert len(parse_json_lines(buf.getvalue())) == 1      # 只有 hello


# ---------------------------------------------------------------- 错误码(前端分类显示用)

def test_error_code_maps_real_exception_types() -> None:
    """错误码由**真实异常类**决定:类名一改这里就红,前端不会静默退化成「未知错误」。"""
    from backends.base import BackendError
    from backends.mineru_backend import MinerUError
    from backends.paddleocr_backend import PaddleOCRError
    from batch import VerifyError
    from epub.pandoc import PandocMissingError
    import pymupdf

    cases: list[tuple[BaseException, str]] = [
        (PandocMissingError("未找到 pandoc"), "missing_dependency"),
        (VerifyError("EPUB 校验未通过"), "verify_failed"),
        (MinerUError("解析失败"), "ocr_failed"),
        (PaddleOCRError("提交异常"), "ocr_failed"),
        (BackendError("其他后端错误"), "backend_failed"),
        (FileNotFoundError("文件不存在: a.pdf"), "input_error"),
        (pymupdf.FileDataError("cannot open broken document"), "input_error"),
        (pymupdf.EmptyFileError("empty file"), "input_error"),
        (PermissionError("拒绝访问"), "output_error"),
        (OSError("磁盘已满"), "output_error"),
        (ValueError("未知清理项: nope"), "config_error"),
        (RuntimeError("Pandoc 失败(exit=3): boom"), "pandoc_error"),
        (RuntimeError("别的意外"), "convert_failed"),
        (TypeError("意外类型"), "convert_failed"),
    ]
    for exc, want in cases:
        assert events.error_code(exc) == want, f"{type(exc).__name__} → {events.error_code(exc)}"
    # 认不出来时用调用方给的码(如 name_conflict),不被抹成 convert_failed
    assert events.error_code(RuntimeError("别的意外"), default="name_conflict") == "name_conflict"


def test_failed_conversion_reports_specific_code(tmp_path: Path, monkeypatch,
                                                 capsys) -> None:
    """后端认证失败 → 事件里的 code 必须是 ocr_failed(而不是笼统的 convert_failed)。"""
    from backends.mineru_backend import MinerUError
    import batch as batch_mod
    from conftest import make_pdf

    pdf = make_pdf(tmp_path / "扫描书.pdf")
    monkeypatch.setattr(
        batch_mod, "_process_pdf",
        lambda *a, **k: (_ for _ in ()).throw(MinerUError("缺少 MinerU API Token", retryable=False)),
    )
    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log", "--json-events"])
    assert rc == 1
    items = parse_json_lines(capsys.readouterr().out)
    errs = events_of(items, "error")
    assert len(errs) == 1 and errs[0]["code"] == "ocr_failed"


def test_retry_is_reported_as_an_event(tmp_path: Path, monkeypatch) -> None:
    """重试要发 `retry` 事件:界面才能把「卡住」与「正在重试」区分开。"""
    import batch as batch_mod
    from conftest import make_pdf

    pdf = make_pdf(tmp_path / "会重试.pdf")
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("临时故障")     # 默认可重试
        raise batch_mod.VerifyError("第二轮的确定性失败")

    monkeypatch.setattr(batch_mod, "_process_pdf", flaky)
    monkeypatch.setattr(batch_mod.time, "sleep", lambda *_: None)

    buf = io.StringIO()
    events.configure(stream=buf)
    try:
        result = batch_mod.process_one(pdf, config={"pymupdf": {}}, work_root=tmp_path / "work",
                                       output_dir=tmp_path / "out", retries=2)
    finally:
        events.configure(False)

    assert result.status == "failed"
    items = parse_json_lines(buf.getvalue())
    retries = events_of(items, "retry")
    assert len(retries) == 1, [i["event"] for i in items]
    assert retries[0]["attempt"] == 1 and retries[0]["retries"] == 2
    assert retries[0]["wait"] == 2          # 2 ** 1 的退避
    assert events_of(items, "error")[0]["code"] == "verify_failed"


def test_skip_is_its_own_terminal_event(tmp_path: Path) -> None:
    """已有产物 → `skip`(不是 `complete`):GUI 必须能把它显示成「已跳过」。"""
    from batch import process_one
    from conftest import make_pdf

    pdf = make_pdf(tmp_path / "重复.pdf")
    kw = dict(retries=0, config={"pymupdf": {"write_images": True, "bold_fonts": []}},
              work_root=tmp_path / "work", output_dir=tmp_path / "out")
    assert process_one(pdf, **kw).status == "done"

    buf = io.StringIO()
    events.configure(stream=buf)
    try:
        result = process_one(pdf, **kw)
    finally:
        events.configure(False)

    assert result.status == "skipped"
    items = parse_json_lines(buf.getvalue())
    assert [i["event"] for i in items if i["event"] != "hello"] == ["skip"]
    assert events_of(items, "complete") == []

    # --force:用户明确要求重转时不能再 skip,而要真的跑完整条链路
    buf2 = io.StringIO()
    events.configure(stream=buf2)
    try:
        forced = process_one(pdf, force=True, **kw)
    finally:
        events.configure(False)
    assert forced.status == "done"
    names = [i["event"] for i in parse_json_lines(buf2.getvalue())]
    assert "complete" in names and "skip" not in names


# ---------------------------------------------------------------- CLI 端到端

def test_cli_stdout_is_pure_json_and_logs_move_to_stderr(tmp_path: Path, capsys) -> None:
    pdf = make_pdf(tmp_path / "本地书.pdf")     # 正文够长 → 判定文字版,不调云端
    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log", "--json-events"])
    assert rc == 0

    cap = capsys.readouterr()
    items = parse_json_lines(cap.out)           # stdout 混进日志就会在这里炸
    names = [i["event"] for i in items]
    assert names[0] == "hello"
    assert "[detect]" not in cap.out and "[detect]" in cap.err, "人类日志必须改走 stderr"

    stages = [(i["name"], i["state"]) for i in events_of(items, "stage")]
    assert ("extract", "start") in stages and ("extract", "done") in stages
    assert ("clean", "start") in stages and ("build", "done") in stages
    assert ("verify", "start") in stages and ("verify", "done") in stages

    detect = events_of(items, "detect")[0]
    assert detect["type"] == "text" and detect["pages"] >= 1
    plan = events_of(items, "plan")[0]
    assert plan["backend"] == "pymupdf" and plan["ocr_pages"] == 0

    verify = events_of(items, "verify")[0]
    assert verify["errors"] == 0 and verify["warnings"] == 0
    complete = items[-1]
    assert complete["event"] == "complete" and complete["pdf_type"] == "text"
    assert complete["epub"].endswith(".epub")
    # 阶段顺序:detect → extract → clean → build → verify(complete 收尾)
    order = [i["name"] for i in events_of(items, "stage") if i["state"] == "start"]
    assert order == ["detect", "extract", "clean", "build", "verify"], order
    # 检测结果事件落在 detect 阶段之内
    assert names.index("stage") < names.index("detect") < names.index("plan") < \
        names.index("complete")


def test_cli_reports_config_errors_as_events(tmp_path: Path, capsys) -> None:
    """配置错误也要发 error 事件(带码 config_error):桌面端据此显示「配置错误」,
    而不是笼统的「转换失败」。

    (与下面那条输入错误的用例分开写:`cli_main` 会把 sys.stdout 换成 stderr 且不还原,
    同一个用例里再调一次,事件就不进 capsys 的 out 了。)
    """
    pdf = make_pdf(tmp_path / "本地书.pdf")
    rc = cli_main([str(pdf), "--clean-disable", "no_such_key", "--json-events", "--no-log"])
    assert rc == 2
    errs = events_of(parse_json_lines(capsys.readouterr().out), "error")
    assert errs and errs[0]["code"] == "config_error", errs


def test_cli_reports_missing_input_as_error_event(tmp_path: Path, capsys) -> None:
    """没有可处理的文件 → error 事件 + input_error(而不是静默 exit=2)。"""
    rc = cli_main([str(tmp_path / "不存在.pdf"), "--json-events", "--no-log"])
    assert rc == 2
    errs = events_of(parse_json_lines(capsys.readouterr().out), "error")
    assert errs and errs[0]["code"] == "input_error", errs
    assert "没有找到可处理的文件" in errs[0]["message"]


# ---------------------------------------------------------------- 预检 vs 实际后端(桌面端 Case E)


def _preflight_plan(capsys, *extra: str) -> dict:
    """跑一次预检(不产出文件)并取第一条计划。"""
    from cli import main as cli_main_local
    assert cli_main_local(["--dry-run", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])["files"][0]


def test_preflight_backend_matches_auto_conversion(tmp_path: Path, capsys) -> None:
    """auto:预检报的后端必须就是实际转换用到的后端(桌面端「印前检查」与
    「实际转换」不能各说一套)。"""
    pdf = make_pdf(tmp_path / "本地书.pdf", pages=3)
    plan = _preflight_plan(capsys, str(pdf))
    assert plan["backend"] == "pymupdf" and plan["kind"] == "pdf-text"

    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log", "--json-events"])
    assert rc == 0
    items = parse_json_lines(capsys.readouterr().out)
    assert events_of(items, "plan")[0]["backend"] == plan["backend"]
    assert events_of(items, "detect")[0]["type"] == "text"


def test_preflight_backend_matches_backend_override(tmp_path: Path, capsys) -> None:
    """`--backend pymupdf`(桌面端设置里的后端偏好):预检与实际转换都必须是它,
    而且实际转换**不发 plan 事件**(壳按带 backend 的阶段事件显示徽标)——
    两边对不上就会出现「UI 显示 local、实际用 cloud」。"""
    pdf = make_pdf(tmp_path / "本地书.pdf", pages=3)
    plan = _preflight_plan(capsys, "--backend", "pymupdf", str(pdf))
    assert plan["backend"] == "pymupdf" and plan["kind"] == "pdf-text"

    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log", "--json-events", "--backend", "pymupdf"])
    assert rc == 0
    items = parse_json_lines(capsys.readouterr().out)
    assert events_of(items, "plan") == [], "显式指定后端时不发 plan(避免与预检口径分叉)"
    backends = {i["backend"] for i in events_of(items, "stage") if "backend" in i}
    assert backends == {"pymupdf"}, backends


def test_preflight_reports_cloud_override(tmp_path: Path, capsys) -> None:
    """云端覆盖:预检如实显示 mineru,并全篇按需 OCR 页数计(不真跑,不消耗额度)。

    (单独一条用例:`cli_main` 会把 sys.stdout 换成 stderr 不还原,预检的输出在那之后就
    进不了 capsys。)
    """
    pdf = make_pdf(tmp_path / "本地书.pdf", pages=3)
    plan = _preflight_plan(capsys, "--backend", "mineru", str(pdf))
    assert plan["backend"] == "mineru" and plan["ocr_pages"] == 3
    assert plan["kind"] == "pdf-scanned"      # 手动指定云端 = 不看文字层检测结果


def test_cli_without_flag_keeps_plain_log(tmp_path: Path, capsys) -> None:
    """未开启事件流:stdout 仍是人类日志(老桌面端/脚本不受影响)。"""
    pdf = make_pdf(tmp_path / "本地书.pdf")
    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log"])
    assert rc == 0
    cap = capsys.readouterr()
    assert "[detect]" in cap.out
    assert not any(json.loads(l).get("event") for l in cap.out.splitlines() if l.strip().startswith("{"))


def test_dry_run_ignores_json_events(tmp_path: Path, capsys) -> None:
    """预检自带结构化 JSON:不被事件流挤到 stderr。"""
    pdf = make_pdf(tmp_path / "本地书.pdf")
    rc = cli_main([str(pdf), "--dry-run", "--json", "--json-events"])
    assert rc == 0
    cap = capsys.readouterr()
    payload = json.loads(cap.out.strip().splitlines()[-1])
    assert payload["files"][0]["kind"] == "pdf-text"   # 预检 JSON 仍在 stdout
    assert "event" not in payload                     # 事件流被忽略,不叠加


def test_hybrid_reports_real_ocr_runs(tmp_path: Path, monkeypatch, capsys) -> None:
    """hybrid 的区段数必须来自真实规划(旧桌面端只能从文案里猜)。"""
    import convert as convert_mod
    from conftest import make_mixed_pdf
    from test_hybrid import CFG, FakeOCR

    # 版面:第 3、6 页是扫描页 → 两段互不相邻的扫描区段
    pdf = make_mixed_pdf(tmp_path / "混合书.pdf", "TTSTTSTTT")
    fake = FakeOCR()
    real_get_backend = convert_mod.get_backend

    def fake_get_backend(name, cfg=None, _f=fake):
        # 文字页仍走真实本地后端,只有云端 OCR 换成假的
        return real_get_backend("pymupdf", cfg or {}) if name == "pymupdf" else _f

    monkeypatch.setattr(convert_mod, "get_backend", fake_get_backend)
    monkeypatch.setattr(convert_mod, "load_config", lambda *a, **k: CFG)

    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log", "--json-events"])
    assert rc == 0
    items = parse_json_lines(capsys.readouterr().out)

    detect = events_of(items, "detect")[0]
    assert detect["type"] == "hybrid" and detect["suspicious_pages"] == 0
    plan = events_of(items, "plan")[0]
    assert plan["backend"].startswith("hybrid(")
    assert plan["ocr_pages"] == 2 and plan["ocr_runs"] == 2     # 两段独立扫描页
    prog = events_of(items, "progress")
    assert [p["current"] for p in prog] == [1, 2]
    assert all(p["total"] == 2 and p["detail"] == "hybrid_ocr" for p in prog)
    assert events_of(items, "complete")[0]["pdf_type"] == "hybrid"


def test_mineru_reports_shards_and_progress(tmp_path: Path, monkeypatch, capsys) -> None:
    """分片数与分片进度来自后端真实提交(复用时序用的假云端服务)。"""
    from backends import mineru_backend
    from test_ocr_sharding_resume import install_fake_cloud

    install_fake_cloud(monkeypatch)     # 假 MinerU:替换 requests/sleep,不发网络请求
    monkeypatch.setattr(mineru_backend, "load_api_key", lambda *a, **k: "fake-token")
    pdf = make_paged_pdf(tmp_path / "扫描书.pdf", 450)
    rc = cli_main([str(pdf), "-o", str(tmp_path / "out"), "--work", str(tmp_path / "work"),
                   "--no-log", "--json-events", "--backend", "mineru"])
    assert rc == 0
    items = parse_json_lines(capsys.readouterr().out)

    shards = events_of(items, "shards")[0]
    assert shards["total"] == 3
    assert shards["ranges"] == ["1-200", "201-400", "401-450"]
    prog = events_of(items, "progress")
    assert [p["current"] for p in prog] == [1, 2, 3] and all(p["total"] == 3 for p in prog)
