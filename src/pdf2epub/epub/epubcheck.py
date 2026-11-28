"""EPUBCheck 集成(可选、非必需依赖)。

**为什么要有**:项目自带的 `epub/verify.py` 证明「包自洽、内容没丢」,但它不是规范
检查器 —— 它不知道 EPUB 3 的 content model。实测就抓到过一类真问题:目录列表曾是
`<ol>`→`<ul>` 的「去编号」改造,自校验一路 OK,而 EPUBCheck 对**每一本产物**报
`RSC-005`(toc 导航里必须是 `ol`)。所以规范检查必须有,但它依赖 Java,不能变成
硬依赖:

- 找得到 `epubcheck.jar` + `java` 才跑(`auto`,默认);找得到就只记警告,不判失败;
- `on`(显式开启)才把 EPUBCheck 的 ERROR 当转换失败;
- `off` 完全跳过。

开关:`config.yaml` 的 `verify.epubcheck`,环境变量 `PDF2EPUB_EPUBCHECK` 覆盖。
Jar 的获取:`uv run python scripts/install_epubcheck.py`。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 显式指定 jar 的环境变量(测试/CI 用)
JAR_ENV = "EPUBCHECK_JAR"
#: 运行模式覆盖:off / auto / on
MODE_ENV = "PDF2EPUB_EPUBCHECK"
#: 模式取值
MODES = ("off", "auto", "on")
#: 一次检查的超时(大书 + 冷启动 JVM 也就几十秒)
TIMEOUT = 240
#: 最多往校验结果里塞多少条(避免一本问题书刷出上千行)
MAX_MESSAGES = 20


@dataclass(frozen=True)
class EpubCheckMessage:
    severity: str
    code: str
    message: str
    path: str = ""
    line: int | None = None

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.path else ""
        return f"{self.code} {where} {self.message}".strip()


@dataclass(frozen=True)
class EpubCheckOutcome:
    """一次 EPUBCheck 的结果。`skipped` 非空表示没跑(不是失败)。"""

    ok: bool
    skipped: str | None = None
    messages: tuple[EpubCheckMessage, ...] = ()

    @property
    def errors(self) -> list[EpubCheckMessage]:
        return [m for m in self.messages if m.severity in ("FATAL", "ERROR")]

    @property
    def warnings(self) -> list[EpubCheckMessage]:
        return [m for m in self.messages if m.severity == "WARNING"]


def resolve_mode(configured: str | None = None) -> str:
    """有效模式:环境变量 > 配置 > auto;非法值退化为 auto(不因为写错开关就不检查)。"""
    for raw in (os.environ.get(MODE_ENV), configured):
        mode = (raw or "").strip().lower()
        if mode in MODES:
            return mode
    return "auto"


def find_epubcheck() -> Path | None:
    """找 epubcheck.jar:环境变量 → 仓库 `.tools/` → 用户数据目录 → PATH 里的命令。"""
    env = os.environ.get(JAR_ENV)
    if env:
        p = Path(env)
        if p.is_file():
            return p
    # 仓库根 = src/pdf2epub/epub/epubcheck.py 的上三级
    root = Path(__file__).resolve().parents[3]
    for base in (root / ".tools" / "epubcheck",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "pdf2epub" / "epubcheck"):
        if not base.is_dir():
            continue
        direct = base / "epubcheck.jar"
        if direct.is_file():
            return direct
        found = sorted(base.rglob("epubcheck.jar"))
        if found:
            return found[0]
    return None


def java_available() -> bool:
    return shutil.which("java") is not None


def parse_report(text: str) -> tuple[EpubCheckMessage, ...]:
    """EPUBCheck 的 JSON 报告 → 消息列表;格式不认识就返回空(不让解析错误打断转换)。"""
    try:
        data = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(data, dict):
        return ()
    out: list[EpubCheckMessage] = []
    for item in data.get("messages") or []:
        if not isinstance(item, dict):
            continue
        locations = item.get("locations") or []
        loc = locations[0] if locations and isinstance(locations[0], dict) else {}
        line = loc.get("line")
        out.append(EpubCheckMessage(
            severity=str(item.get("severity") or "").upper(),
            code=str(item.get("ID") or ""),
            message=str(item.get("message") or ""),
            path=str(loc.get("path") or ""),
            line=line if isinstance(line, int) else None,
        ))
    return tuple(out)


def run_epubcheck(epub: str | Path, *, jar: Path | None = None) -> EpubCheckOutcome:
    """跑一次 EPUBCheck;没装(或没 Java)时返回 skipped,绝不抛异常。"""
    jar = jar or find_epubcheck()
    if jar is None:
        return EpubCheckOutcome(ok=True, skipped="epubcheck-missing")
    if not java_available():
        return EpubCheckOutcome(ok=True, skipped="java-missing")
    with tempfile.TemporaryDirectory(prefix="pdf2epub-epubcheck-") as td:
        report = Path(td) / "report.json"
        try:
            proc = subprocess.run(
                ["java", "-jar", str(jar), "-j", str(report), str(epub)],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return EpubCheckOutcome(ok=False, messages=(EpubCheckMessage(
                severity="ERROR", code="epubcheck_crashed", message=str(exc)),))
        if report.is_file():
            messages = parse_report(report.read_text(encoding="utf-8", errors="replace"))
        else:
            messages = ()
        return EpubCheckOutcome(ok=proc.returncode == 0, messages=messages)
