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
