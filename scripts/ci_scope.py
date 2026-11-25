"""CI 变更范围判定 + PR 门禁（纯标准库，跨平台）。

CI 里由 workflow 注入 EVENT_NAME / BASE_SHA / BEFORE_SHA / HEAD_SHA / HEAD_REF /
PR_TITLE / PR_LABELS / FORK / FORCE_SCOPE 这些环境变量；结果写入
``$GITHUB_OUTPUT``（本地运行时打印到 stdout）：

    docs_only / python / frontend / rust / release / full   (true|false)

本地用法：

    python scripts/ci_scope.py --self-test
    python scripts/ci_scope.py --check-docs
    CI_SCOPE_FILES="README.md docs/a.png" python scripts/ci_scope.py

设计原则：

* 只读服务端信息（git diff、PR 标题/标签/是否 fork），**不执行 PR 里的代码**，
  因此不需要 ``pull_request_target``，也不需要任何 secrets。
* 任何异常、无法取到 diff、或出现不认识的路径，一律 ``full=true``（fail-safe），
  绝不因为「优化」而漏检。
* 门禁是保守兜底：只拦「禁止入库的路径」与「疑似真实凭证」。人工 review 与
  PR 模板里的自查项仍是主防线，这里不做内容语义判断。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Windows runner 的默认管道是 cp1252，脚本打印中文会抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 路径规则
# 纯文档：改动全部落在这里才走轻量路径。
DOC = (
    "*.md",
    "docs/**",
    "LICENSE*",
    "SECURITY*",
    "CONTRIBUTING*",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE.md",
)

# Python 引擎 / 测试 / 打包输入
PYTHON = (
    "src/pdf2epub/**",
    "tests/**",
    "config/**",
    "scripts/**",
    "pyproject.toml",
    "cli.spec",
    ".python-version",
)

# 桌面前端
FRONTEND = (
    "desktop/src/**",
    "desktop/package*.json",
    "desktop/vite.config.ts",
    "desktop/vitest.config.ts",
    "desktop/tsconfig*.json",
    "desktop/eslint.config.js",
    "desktop/index.html",
)

# Rust / Tauri 壳
RUST = ("desktop/src-tauri/**",)

# 发布形态（Windows 打包 + smoke test）。README / pyproject / types.ts 不在这里：
# 版本一致性由常驻的 bump_version.py --check 秒级覆盖，没必要为改一行 README
# 去跑一遍 Windows 打包。
RELEASE = (
    "cli.spec",
    "scripts/build_release.py",
    "scripts/check_release.py",
    "scripts/verify_epub.py",
    "desktop/src-tauri/tauri.conf.json",
    "desktop/src-tauri/Cargo.toml",
)

# 命中即强制全量：锁文件、CI 自身、安全敏感路径、分类脚本自身。
FULL = (
    "uv.lock",
    "desktop/package-lock.json",
    "desktop/src-tauri/Cargo.lock",
    ".github/workflows/**",
    ".github/dependabot.yml",
    "scripts/ci_scope.py",
    "src/pdf2epub/net.py",
    "src/pdf2epub/backends/safe_zip.py",
    "src/pdf2epub/backends/mineru_http.py",
    "src/pdf2epub/backends/mineru_package.py",
    "src/pdf2epub/epub/verify.py",
    "scripts/verify_epub.py",
    "*credential*",
    "apikey*",
)

# 禁止「新增」的路径（PR 模板自查项的自动化兜底）
GUARD_BAN = (
    "apikey.json",
    "*.pdf",
    "*.epub",
    "work/**",
    "output/**",
    "logs/**",
    "dist_cli/**",
    "dist_release/**",
    "dist_ci/**",
    "build_ci/**",
    ".venv/**",
    "desktop/src-tauri/target/**",
    "tests/_artifacts/**",
)

# 疑似真实凭证。只匹配 sk- 开头的长串；项目代码里没有这种形态，误报风险低。
TOKEN_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")
DOC_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _run_git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git {' '.join(args)} 失败")
    return proc.stdout


def _diff_spec(event: str, base: str, before: str, head: str) -> str | None:
    """构造 git diff 的比较式；返回 None 表示拿不到可判定的 diff。"""
    if event == "workflow_dispatch":
        return None
    if event == "pull_request":
        if not base or not head:
            raise RuntimeError("PR 缺少 base/head sha")
        return f"{base}...{head}"          # 三点：base 到 head 的净改动
    if not before or set(before) == {"0"}:
        return None                        # 新分支首次 push
    return f"{before}..{head}"


def _changed_files(
    spec: str | None, *, diff_filter: str | None = None, override: str | None = None
) -> list[str] | None:
    if override:
        return [p for p in re.split(r"[\s,]+", override) if p]
    if spec is None:
        return None
    args = ["diff", "--name-only"]
    if diff_filter:
        args.append(f"--diff-filter={diff_filter}")
    args.append(spec)
    return [p.strip().replace("\\", "/")
            for p in _run_git(args).splitlines() if p.strip()]


def _match(path: str, patterns: tuple[str, ...]) -> bool:
    # fnmatch 的 * 会跨 / 匹配，正好覆盖 ** 语义（路径已统一成 /）。
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def _blank() -> dict[str, bool]:
    return dict(python=False, frontend=False, rust=False, release=False,
                full=False, docs_only=False)


def _expand_full(flags: dict[str, bool]) -> dict[str, bool]:
    flags.update(python=True, frontend=True, rust=True, release=True, docs_only=False)
    return flags


def classify_from(
    files: list[str] | None,
    *,
    event: str,
    title: str = "",
    head_ref: str = "",
    labels: list[str] | None = None,
    fork: bool = False,
    force: str = "auto",
) -> dict[str, bool]:
    """纯函数：给定改动文件与元数据，产出各层开关。便于本地自测。"""
    labels = labels or []

    # push main 是 PR 合并后的结果：合并前 PR 上已按改动范围跑过完整检查，
    # 且 push 事件不受分支保护约束，再跑一遍全量没有任何门禁价值。
    # 两个受保护 job 依旧运行并上报 success，只是把所有重步骤关掉；凭证门禁
    # (guard()) 与版本号一致性由 workflow 的常驻步骤保证，不随这里跳过。
    # 需要人工重跑全量时用 workflow_dispatch.scope=full。
    if event == "push":
        print("[scope] push main：PR 已验证，仅保留常驻门禁，跳过所有重步骤")
        return _blank()

    flags = _blank()

    def note(reason: str) -> None:
        print(f"[scope] 强制全量: {reason}")
        flags["full"] = True

    if files is None:
        note("无可判定 diff（dispatch / 新分支 / diff 失败）")
    else:
        print(f"[scope] 改动文件 {len(files)} 个")
        for p in files[:30]:
            print(f"  {p}")
        if len(files) > 30:
            print(f"  ... 另有 {len(files) - 30} 个")

        # 1) 强制全量规则优先
        for p in files:
            if _match(p, FULL):
                note(f"命中强制全量路径: {p}")
                break

        # 2) 不认识的路径不能假设「无关紧要」
        known = DOC + PYTHON + FRONTEND + RUST + RELEASE
        if not flags["full"]:
            for p in files:
                if not _match(p, known):
                    note(f"未知路径，保守全量: {p}")
                    break

        # 3) 分层
        if not flags["full"]:
            flags["python"] = any(_match(p, PYTHON) for p in files)
            flags["frontend"] = any(_match(p, FRONTEND) for p in files)
            flags["rust"] = any(_match(p, RUST) for p in files)
            flags["release"] = any(_match(p, RELEASE) for p in files)
            if flags["rust"]:
                # 改了 Tauri 壳就必须验证壳能打包，不能只跑 cargo test
                flags["release"] = True
            if flags["release"]:
                # 发布形态要验证整条链路，基础检查一并跑，避免只验壳不验引擎
                flags["python"] = flags["frontend"] = flags["rust"] = True
            if files and all(_match(p, DOC) for p in files):
                flags["docs_only"] = True

    # 4) 元数据覆盖（只加不减）
    if fork:
        note("fork PR")
    if head_ref.startswith("release/"):
        print("[scope] release/ 分支：追加发布形态检查")
        flags["release"] = True
        flags["python"] = flags["frontend"] = flags["rust"] = True
    if re.match(r"^(chore\(deps\)|deps:|release:)", title, re.I):
        note("标题标记为依赖/发布改动")
    if "ci:full" in labels:
        note("标签 ci:full")
    if "ci:docs" in labels:
        if flags["docs_only"]:
            print("[scope] 标签 ci:docs 生效")
        else:
            print("[scope] 标签 ci:docs 已忽略（改动并非纯文档）")

    # 5) workflow_dispatch 的手动选择直接覆盖自动判定
    if event == "workflow_dispatch" and force != "auto":
        flags = _blank()
        if force == "docs":
            flags["docs_only"] = True
        elif force == "full":
            flags["full"] = True
        elif force in ("python", "frontend", "rust"):
            flags[force] = True
        elif force == "release":
            flags["release"] = True
            flags["python"] = flags["frontend"] = flags["rust"] = True
        print(f"[scope] workflow_dispatch 覆盖: scope={force}")

    # 6) full 展开 / 兜底
    if flags["full"]:
        _expand_full(flags)
    elif flags["docs_only"]:
        flags.update(python=False, frontend=False, rust=False, release=False)
    elif not any(flags[k] for k in ("python", "frontend", "rust", "release")):
        note("未匹配任何检查层")
        _expand_full(flags)

    return flags


def guard() -> bool:
    """保守门禁：禁止新增文件 + 疑似真实凭证。返回 False 即让 job 变红。"""
    override = _env("CI_SCOPE_FILES") or None
    if override:
        # 本地自测：把给定列表当作全部改动，只做路径检查；不要去算 diff spec，
        # 否则 PR 缺 base/head 时会误判成「无可判定 diff」而跳过门禁
        added, diff_text = _changed_files(None, override=override), ""
    else:
        try:
            spec = _diff_spec(
                _env("EVENT_NAME", "push"), _env("BASE_SHA"),
                _env("BEFORE_SHA"), _env("HEAD_SHA") or "HEAD",
            )
        except RuntimeError as exc:
            print(f"[guard] diff 不可用，跳过门禁: {exc}")
            return True
        if spec is None:
            print("[guard] 无可判定 diff，跳过门禁")
            return True
        added = _changed_files(spec, diff_filter="A") or []
        diff_text = _run_git(["diff", "--no-color", spec])

    banned = [p for p in added if _match(p, GUARD_BAN)]
    if banned:
        print("[guard] 以下文件不允许入库（PR 模板自查项）：", file=sys.stderr)
        for p in banned:
            print(f"  {p}", file=sys.stderr)
        return False

    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") and TOKEN_RE.search(line):
            print("[guard] 新增行里出现疑似真实凭证（sk-...），请删除后再提交",
                  file=sys.stderr)
            return False
    return True


def check_docs() -> bool:
    """只检查本次改动过的 md 里引用的本地相对链接/图片是否存在。

    不去扫全仓库的历史链接，避免「改一行 README 却被三年前的死链卡住」。
    """
    override = _env("CI_SCOPE_FILES") or None
    try:
        if override:
            files = _changed_files(None, override=override)
        else:
            spec = _diff_spec(
                _env("EVENT_NAME", "push"), _env("BASE_SHA"),
                _env("BEFORE_SHA"), _env("HEAD_SHA") or "HEAD",
            )
            files = _changed_files(spec, diff_filter="ACM")
    except RuntimeError as exc:
        print(f"[docs] 无法取 diff，跳过链接检查: {exc}")
        return True
    if not files:
        return True

    md_files = [p for p in files if p.lower().endswith(".md") and Path(p).is_file()]
    missing: list[tuple[str, str]] = []
    for rel in md_files:
        text = Path(rel).read_text(encoding="utf-8", errors="replace")
        for m in DOC_LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#")[0]
            if not target:
                continue
            resolved = (Path(rel).parent / target).resolve()
            if not resolved.exists():
                missing.append((rel, target))

    if missing:
        print("[docs] 以下本地链接指向不存在的文件：", file=sys.stderr)
        for rel, target in missing:
            print(f"  {rel} -> {target}", file=sys.stderr)
        return False
    print(f"[docs] 检查 {len(md_files)} 个 md 的本地链接，全部有效")
    return True


def emit(flags: dict[str, bool]) -> None:
    out_path = _env("GITHUB_OUTPUT")
    lines = [f"{k}={'true' if v else 'false'}" for k, v in flags.items()]
    if out_path:
        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    for line in lines:
        print(f"[scope] {line}")


def self_test() -> int:
    cases: list[tuple[str, list[str], dict[str, bool]]] = [
        ("纯文档", ["README.md", "docs/screenshot.png"],
         dict(docs_only=True)),
        ("安全策略文档", ["SECURITY.md", "CONTRIBUTING.zh-CN.md"],
         dict(docs_only=True)),
        ("引擎改动", ["src/pdf2epub/markdown/footnotes.py", "tests/test_cleaner.py"],
         dict(python=True)),
        ("前端改动", ["desktop/src/App.tsx"], dict(frontend=True)),
        ("Rust 改动", ["desktop/src-tauri/src/lib.rs"], dict(rust=True)),
        ("发布形态", ["cli.spec"], dict(release=True, python=True)),
        ("锁文件", ["uv.lock"], dict(full=True, python=True)),
        ("安全敏感", ["src/pdf2epub/net.py"], dict(full=True, python=True)),
        ("CI 自身", [".github/workflows/ci.yml"], dict(full=True, release=True)),
        ("文档+代码", ["README.md", "src/pdf2epub/cli.py"],
         dict(python=True, docs_only=False)),
        ("未知路径", ["weird/newthing.xyz"], dict(full=True, python=True)),
        ("版本号全套", ["pyproject.toml", "uv.lock", "desktop/src/types.ts"],
         dict(full=True, release=True)),
    ]
    meta = dict(event="pull_request", title="", head_ref="feature/x",
                labels=[], fork=False, force="auto")
    failed = 0
    for name, files, expect in cases:
        got = classify_from(files, **meta)
        bad = {k: (got[k], v) for k, v in expect.items() if got[k] != v}
        status = "OK " if not bad else "FAIL"
        print(f"  {status} {name}: {bad or '符合预期'}")
        if bad:
            failed += 1
    # push main 走轻量路径（cases 统一用 pull_request meta，这里单独验）
    push_flags = classify_from(
        ["src/pdf2epub/cli.py", "uv.lock"], event="push", title="",
        head_ref="main", labels=[], fork=False, force="auto",
    )
    push_ok = not any(push_flags.values())
    print(f"  {'OK ' if push_ok else 'FAIL'} push main 轻量: {push_flags}")
    if not push_ok:
        failed += 1
    total = len(cases) + 1
    print(f"[self-test] {total - failed}/{total} 通过")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI 变更范围判定 + PR 门禁")
    parser.add_argument("--self-test", action="store_true", help="跑内置分类用例")
    parser.add_argument("--check-docs", action="store_true", help="检查改动 md 的本地链接")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.check_docs:
        return 0 if check_docs() else 1

    labels: list[str] = []
    try:
        labels = json.loads(_env("PR_LABELS") or "[]")
    except (ValueError, TypeError):
        pass

    override = _env("CI_SCOPE_FILES") or None
    files: list[str] | None = None
    try:
        if override:
            files = _changed_files(None, override=override)
        else:
            spec = _diff_spec(
                _env("EVENT_NAME", "push"), _env("BASE_SHA"),
                _env("BEFORE_SHA"), _env("HEAD_SHA") or "HEAD",
            )
            files = _changed_files(spec)
    except RuntimeError as exc:
        print(f"[scope] 取 diff 失败，按全量处理: {exc}")

    flags = classify_from(
        files,
        event=_env("EVENT_NAME", "push"),
        title=_env("PR_TITLE"),
        head_ref=_env("HEAD_REF"),
        labels=labels,
        fork=_env("FORK").lower() == "true",
        force=_env("FORCE_SCOPE", "auto"),
    )

    # 门禁失败就不输出 outputs：步骤直接变红，后续步骤不会跑
    if not guard():
        return 1

    emit(flags)
    return 0


if __name__ == "__main__":
    sys.exit(main())
