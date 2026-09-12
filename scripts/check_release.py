"""发布包自检(计划 1.4):解压 → 实跑引擎 → 结构校验。

    uv run python scripts/check_release.py dist_release/pdf2epub-v0.2.4-win-x64.zip
    uv run python scripts/check_release.py dist_release/v0.2.4        # 也可给已解压目录

检查项:
  1. 包内必备文件齐全(pdf2epub.exe / cli.exe / config/config.yaml / config/book.css)
  2. 在**别的目录**用绝对路径调用 cli.exe 能把 PDF 转成 EPUB(退出码 0)
  3. 产物通过 EPUB 结构校验(0 失败)
  4. cli.exe 取到的是**同级 config/book.css**(改一处标记后重跑,与 EPUB 内嵌 CSS 比对)
     —— 这一项同时验证「绿色版不依赖 cwd」的配置定位
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from epub.verify import verify_epub   # noqa: E402

REQUIRED = ("pdf2epub.exe", "cli.exe", "config/config.yaml", "config/book.css")
CSS_MARKER = "\n/* release-check: config 定位标记 */\n"


def _sha256(text: str) -> str:
    """行尾归一后的 sha256(排除 CRLF/LF 差异)。"""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _make_test_pdf(path: Path) -> Path:
    """现场生成一个中文测试 PDF(不依赖项目样本,发布包自检要能独立运行)。"""
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    img = path.parent / "fig.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 32, 32))
    pix.set_rect(pix.irect, (20, 120, 220))
    pix.save(img)

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(60, 60, 535, 100), "第一章 发布包自检",
                        fontname="china-s", fontsize=18)
    page.insert_textbox(
        pymupdf.Rect(60, 110, 535, 220),
        "这是发布包自检生成的正文段落。它的长度足以让类型检测判定为文字版 PDF,"
        "从而走本地提取路径,不消耗任何云端 OCR 额度,也不需要网络与凭证。"
        "本段用于验证绿色版引擎可以独立完成转换,并以句号结尾。",
        fontname="china-s", fontsize=11, lineheight=1.6,
    )
    page.insert_image(pymupdf.Rect(60, 240, 260, 390), filename=str(img))
    doc.save(path)
    doc.close()
    return path


def _run_engine(app_dir: Path, pdf: Path, out_dir: Path, cwd: Path) -> subprocess.CompletedProcess:
    """用绝对路径调用 cli.exe,cwd 故意选在解压目录之外。

    显式 `--backend pymupdf`:发布包自检必须能在**无网络/无云端凭证**的机器上跑通
    (自检验证的是「引擎可独立完成转换 + 产物结构 + 配置定位」,不是云端 OCR)。
    """
    cmd = [str(app_dir / "cli.exe"), str(pdf), "-o", str(out_dir),
           "--backend", "pymupdf",
           "--force", "--no-log", "--strict"]
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _embedded_css(epub: Path) -> str:
    """取出 EPUB 内嵌的第一个 CSS 文本。"""
    with zipfile.ZipFile(epub) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".css")]
        if not names:
            raise AssertionError("EPUB 内没有 CSS")
        return zf.read(names[0]).decode("utf-8", errors="replace")


def check(target: Path, *, keep_tmp: bool = False) -> int:
    target = Path(target)
    failures: list[str] = []

    def ok(msg: str) -> None:
        print(f"  [ OK ] {msg}")

    def fail(msg: str) -> None:
        failures.append(msg)
        print(f"  [FAIL] {msg}")

    tmp = Path(tempfile.mkdtemp(prefix="pdf2epub-release-check-"))
    print(f"自检临时目录: {tmp}")

    # ---- 1. 解压与必备文件 ----
    print("\n[1] 包内容")
    if target.is_dir():
        app_dir = target
    elif target.suffix.lower() == ".zip":
        app_dir = tmp / "app"
        app_dir.mkdir()
        with zipfile.ZipFile(target) as zf:
            zf.extractall(app_dir)
        ok(f"解压发布包: {target.name}")
    else:
        fail(f"无法识别的目标: {target}")
        return 1

    for rel in REQUIRED:
        path = app_dir / rel
        if path.exists() and path.stat().st_size > 0:
            ok(f"{rel} ({path.stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            fail(f"缺少或为空: {rel}")
    if failures:
        return 1

    # ---- 2. 实跑引擎(样本与输出都在解压目录之外) ----
    print("\n[2] 实跑引擎(绝对路径调用,cwd 在解压目录之外)")
    work_area = tmp / "user-work"
    pdf = _make_test_pdf(work_area / "books" / "自检样本.pdf")
    out_dir = work_area / "output"
    proc = _run_engine(app_dir, pdf, out_dir, cwd=work_area)
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print(f"    | {line}")
    if proc.returncode != 0:
        fail(f"cli.exe 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
        return 1
    ok("cli.exe 退出码 0")

    epubs = list(out_dir.glob("*.epub"))
    if not epubs:
        fail("没有产出 EPUB")
        return 1
    epub = epubs[0]
    ok(f"产出 {epub.name} ({epub.stat().st_size / 1024:.0f} KB)")

    # ---- 3. 结构校验 ----
    print("\n[3] EPUB 结构校验")
    result = verify_epub(epub)
    for issue in result.errors:
        fail(issue.message)
    for issue in result.warnings:
        print(f"  [WARN] {issue.message}")
    if result.ok:
        ok(f"{result.summary()}:MathML {result.stats.get('math', 0)}, "
           f"图片 {result.stats.get('img_refs', 0)}, CSS {result.stats.get('css', 0)}")

    # ---- 4. 配置定位:改动同级 book.css 必须生效 ----
    print("\n[4] config 定位(exe 同级 config/book.css)")
    css_path = app_dir / "config" / "book.css"
    original = css_path.read_text(encoding="utf-8")
    css_path.write_text(original + CSS_MARKER, encoding="utf-8")
    try:
        proc2 = _run_engine(app_dir, pdf, out_dir, cwd=work_area)
        if proc2.returncode != 0:
            fail(f"改动 book.css 后重跑失败(exit={proc2.returncode})")
        else:
            embedded = _embedded_css(epub)
            if _sha256(embedded) == _sha256(css_path.read_text(encoding="utf-8")):
                ok("EPUB 内嵌 CSS 与 exe 同级 config/book.css 完全一致(sha256)")
            else:
                fail("EPUB 内嵌 CSS 与同级 config/book.css 不一致 —— 配置定位没有走 exe 同级目录")
    finally:
        css_path.write_text(original, encoding="utf-8")

    print(f"\n=== 发布包自检: {len(failures)} 失败 ===")
    if failures:
        print("保留临时目录以便排查:", tmp)
        for f in failures:
            print("  ✗", f)
        return 1
    if not keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    print("发布包可用 ✓")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="pdf2epub 发布包自检")
    parser.add_argument("target", type=Path, help="zip 发布包或已解压目录")
    parser.add_argument("--keep-tmp", action="store_true", help="保留临时目录")
    args = parser.parse_args()
    return check(args.target, keep_tmp=args.keep_tmp)


if __name__ == "__main__":
    sys.exit(main())
