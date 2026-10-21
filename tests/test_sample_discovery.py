"""``work/`` 样本自动发现的规则:必须确定、可复现,且缺样本时要**报错**。

离线验收脚本原先把书名(带书源标记)和本机书库目录写死在脚本里 —— 公开仓库不该
出现这些。改成从 ``work/`` 的结构自动发现之后,发现规则本身就成了验收的一部分:
换成另一台机器、另一套书,只要目录结构一样,选中的样本就必须一样;一本都找不到
时必须停下来报错,而不是悄悄跳过。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from conftest import PROJECT_ROOT

TOOLS = PROJECT_ROOT / ".workbuddy" / "tools"
MODULE_PATH = TOOLS / "sample_discovery.py"

#: 书源站点标记的**形状**(不写全称 —— 免得把那串字又带回公开仓库)。
#: 中间必须有分隔符:既能命中书源站点那几种写法,又不会把 import zlib 误判成标记。
PROVENANCE_RES = (re.compile(r"z[-_.]lib", re.IGNORECASE), re.compile(r"\dlib\b"))


def _cached_book_names() -> list[str]:
    """work/ 下**带 _parts/** 的目录 = 真实缓存书(其余是测试夹具,不算书目)。"""
    work = PROJECT_ROOT / "work"
    if not work.is_dir():
        return []
    return sorted(p.name for p in work.iterdir()
                  if p.is_dir() and (p / "_parts").is_dir())


@pytest.fixture(scope="module")
def disc():
    """按文件路径加载 ``sample_discovery``(``.workbuddy`` 不是合法包名)。"""
    if not MODULE_PATH.is_file():
        pytest.skip("没有 .workbuddy/tools/sample_discovery.py(源码包未带验收工具)")
    spec = importlib.util.spec_from_file_location("sample_discovery_uut", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 必须先登记:dataclass 生成 __init__ 时要按 __module__ 回查 sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _book(root: Path, name: str, parts: dict[str, tuple[int, str | None]],
          *, book_md: bool = True, meta_backend: str | None = None) -> Path:
    """造一本书:``parts`` = {段名: (full.md 字节数, backend 或 None)}。"""
    d = root / name
    (d / "_parts").mkdir(parents=True, exist_ok=True)
    if book_md:
        (d / "book.md").write_text("# 标题\n\n正文。\n", encoding="utf-8")
    if meta_backend is not None:
        (d / "ir").mkdir(parents=True, exist_ok=True)
        (d / "ir" / "meta.json").write_text(
            json.dumps({"backend": meta_backend}), encoding="utf-8")
    for part_id, (size, backend) in parts.items():
        p = d / "_parts" / part_id
        p.mkdir(parents=True, exist_ok=True)
        (p / "full.md").write_text("x" * size, encoding="utf-8")
        if backend is not None:
            (p / ".part.json").write_text(
                json.dumps({"backend": backend}), encoding="utf-8")
    return d


# ------------------------------------------------------------------ 发现规则

def test_only_dirs_with_book_md_and_part_full_md_qualify(disc, tmp_path) -> None:
    """筛选只看结构(book.md + _parts/<段>/full.md),不看目录名叫什么。"""
    _book(tmp_path, "甲", {"whole": (500, "mineru")})
    _book(tmp_path, "没有正文的书", {"whole": (500, "mineru")}, book_md=False)
    _book(tmp_path, "没有分段的书", {})
    (tmp_path / "随手放的笔记.md").write_text("x", encoding="utf-8")

    found = disc.discover_samples(tmp_path)
    assert [s.name for s in found] == ["甲"]


def test_part_is_largest_full_md(disc, tmp_path) -> None:
    """主段 = full.md 最大的那个段:整本段必然不小于任何子段。"""
    _book(tmp_path, "书", {"1-8": (900, "mineru"), "whole": (9000, "paddleocr")})
    sample = disc.discover_samples(tmp_path)[0]
    assert sample.part_id == "whole"
    assert sample.backend == "paddleocr"     # 后端跟着主段走


def test_part_tie_breaks_by_part_id(disc, tmp_path) -> None:
    """字节数相同时按段名字典序取最小 —— 不能依赖遍历顺序。"""
    _book(tmp_path, "书", {"b-part": (100, "mineru"), "a-part": (100, "mineru")})
    assert disc.discover_samples(tmp_path)[0].part_id == "a-part"


def test_backend_falls_back_to_ir_meta(disc, tmp_path) -> None:
    """.part.json 没写 backend 时,回退到 ir/meta.json。"""
    _book(tmp_path, "书", {"whole": (100, None)}, meta_backend="mineru")
    assert disc.discover_samples(tmp_path)[0].backend == "mineru"


def test_backend_unknown_when_nothing_records_it(disc, tmp_path) -> None:
    _book(tmp_path, "书", {"whole": (100, None)})
    assert disc.discover_samples(tmp_path)[0].backend == disc.UNKNOWN_BACKEND


def test_order_is_size_desc_then_name(disc, tmp_path) -> None:
    """排序必须可复现:篇幅降序,平手按目录名升序。"""
    _book(tmp_path, "小书", {"whole": (100, "mineru")})
    _book(tmp_path, "大书", {"whole": (900, "mineru")})
    _book(tmp_path, "另一本一样大的书", {"whole": (900, "mineru")})
    names = [s.name for s in disc.discover_samples(tmp_path)]
    # 900 字节那两本排在前面,且按目录名升序;100 字节的排在最后
    assert names[-1] == "小书"
    assert {names[0], names[1]} == {"大书", "另一本一样大的书"}
    assert names[:2] == sorted(names[:2])


def test_discover_books_returns_legacy_triples(disc, tmp_path) -> None:
    """旧脚本用的 (书目录, 段 id, 后端) 三元组仍然拿得到。"""
    _book(tmp_path, "书", {"whole": (300, "paddleocr")})
    assert disc.discover_books(tmp_path) == [("书", "whole", "paddleocr")]


def test_paths_point_at_real_files(disc, tmp_path) -> None:
    """Sample 暴露的路径必须真的能读到,免得脚本自己去拼字符串。"""
    _book(tmp_path, "书", {"whole": (300, "paddleocr")})
    s = disc.discover_samples(tmp_path)[0]
    assert s.book_md.is_file() and s.full_md.is_file()
    assert s.part_dir == tmp_path / "书" / "_parts" / "whole"
    assert s.content_list == s.part_dir / "content_list.json"


# ------------------------------------------------------------ 缺样本要报错

def test_require_samples_raises_on_empty_work(disc, tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        disc.require_samples(tmp_path)
    assert "book.md" in str(exc.value)       # 错误信息要说清缺什么


def test_require_samples_raises_when_minimum_not_met(disc, tmp_path) -> None:
    _book(tmp_path, "只有一本", {"whole": (100, "mineru")})
    assert len(disc.require_samples(tmp_path, minimum=1)) == 1
    with pytest.raises(SystemExit):
        disc.require_samples(tmp_path, minimum=2)


def test_missing_work_dir_raises_instead_of_silently_passing(disc, tmp_path) -> None:
    """work/ 整个不存在时也不能返回空列表 —— 那会让验收"以为自己跑过了"。"""
    with pytest.raises(SystemExit):
        disc.require_samples(tmp_path / "不存在的目录")


# ------------------------------------------------------------ 公开仓库卫生

def test_offline_scripts_carry_no_provenance_marker() -> None:
    """验收脚本里不许再出现书源标记:公开仓库不声明测试语料从哪来。"""
    if not TOOLS.is_dir():
        pytest.skip("没有 .workbuddy/tools")
    offenders = []
    for script in sorted(TOOLS.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        for pattern in PROVENANCE_RES:
            if pattern.search(text):
                offenders.append(f"{script.name}: /{pattern.pattern}/")
    assert not offenders, "验收脚本里出现了书源标记:\n" + "\n".join(offenders)


def test_offline_scripts_carry_no_full_cached_book_name() -> None:
    """缓存书的**完整目录名**不许出现在脚本里(前缀通配可以,整串不行)。"""
    names = _cached_book_names()
    if not names:
        pytest.skip("没有 work/ 缓存书(源码包不含 work/)")
    offenders = []
    for script in sorted(TOOLS.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        for name in names:
            if name in text:
                offenders.append(f"{script.name}: {name}")
    assert not offenders, "验收脚本里写死了完整书目目录名:\n" + "\n".join(offenders)


def test_offline_scripts_carry_no_machine_specific_home_path() -> None:
    """脚本里不许写死某个用户的家目录/书库路径。"""
    if not TOOLS.is_dir():
        pytest.skip("没有 .workbuddy/tools")
    offenders = []
    for script in sorted(TOOLS.glob("*.py")):
        for i, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue          # 注释里解释"为什么不写死"不算
            if "Path.home()" in line or "USERPROFILE" in line or "os.environ['HOME" in line:
                offenders.append(f"{script.name}:{i}: {line.strip()}")
    assert not offenders, "验收脚本里写死了本机路径:\n" + "\n".join(offenders)
