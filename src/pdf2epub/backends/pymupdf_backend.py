"""PyMuPDF4LLM 后端:电子版 PDF → 统一 Markdown + images/。

输入:具有可靠文字层的电子版 PDF
输出:work_dir/book.md + work_dir/images/(引用路径规范为相对 images/xxx.png)
     + work_dir/evidence.json(段落级证据层,带页号)

关键参数(实测):
- write_images=True 必须显式开启,默认不提取图片
- image_path 指定图片输出目录;markdown 引用前缀需后处理为相对路径
- page_chunks=True 必须开启:整本一次提取时页边界**整段消失**,产物里既没有页码
  注释、也没有证据层 → 跨页断行永远拼不上、页眉页脚无从判定(实测 4 页的电子书
  三处句中页界原样留在正文里)。逐页提取才拿得到 1-indexed 页号。

证据层的来源:PyMuPDF 没有 ``content_list``,这里按**同一份逐页 Markdown** 切出
段落级条目(每块一个 ``page_idx``)。它不提供版面坐标与标题层级 —— 只回答「这段
文字属于第几页」,而这正是页归属、页眉页脚与跨页拼接唯一缺的那一条事实。
故意**不**把 ``#`` 行标成标题:标题裁决层一旦介入,会把 h1/h2 一并归一成同一级
(书名变章),电子书本来就有后端给的标题层级,不需要它重排。
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf4llm

from pdf2epub.markdown.bold import annotate_bold
from pdf2epub.page_result import PageResult, merge_page_results

from .base import Backend, ConversionResult, normalize_image_refs, write_local_evidence

IMAGES_DIR = "images"

#: 段落切分:空行(允许行内有空白)
_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n")
#: 独占一段的图片引用
_IMAGE_LINE_RE = re.compile(r"^!\[[^\]]*\]\((?P<ref>[^)]+)\)[ \t]*$")
#: 独占一段的 Markdown 标题行(证据里只存文字,见模块文档)
_HEADING_LINE_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*\S)[ \t]*$")


def page_evidence(md: str, page_idx: int) -> list[dict]:
    """一页 Markdown → 证据层条目(``page_idx`` 为 0-indexed)。

    粒度是**段落**:对齐拿块的前 24 字当锚点,证据切得比块细(逐行)时短首句就配
    不上;切成同一份段落则锚点必然相等。
    """
    items: list[dict] = []
    for raw in _BLOCK_SPLIT_RE.split(md or ""):
        block = raw.strip()
        if not block:
            continue
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if len(lines) == 1:
            image = _IMAGE_LINE_RE.match(lines[0])
            if image:
                items.append({"type": "image", "img_path": image.group("ref"),
                              "page_idx": page_idx})
                continue
            heading = _HEADING_LINE_RE.match(lines[0])
            if heading:
                items.append({"type": "text", "text": heading.group("text"),
                              "page_idx": page_idx})
                continue
        items.append({"type": "text", "text": block, "page_idx": page_idx})
    return items


class PyMuPDFBackend(Backend):
    name = "pymupdf"

    def __init__(self, write_images: bool = True, bold_fonts: list[str] | None = None) -> None:
        self.write_images = write_images
        self.bold_fonts = bold_fonts or []

    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        """将电子版 PDF 转换为统一 Markdown + images/ + evidence.json。

        逐页提取(``page_chunks=True``)而不是整本一次:页边界是页码注释、页归属与
        跨页拼接的全部依据,整本一次提取会把它整段丢掉(见模块文档)。
        """
        pdf_path = Path(pdf_path)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        images_abs = work_dir / IMAGES_DIR
        images_abs.mkdir(parents=True, exist_ok=True)

        chunks = pymupdf4llm.to_markdown(
            str(pdf_path),
            write_images=self.write_images,
            image_path=str(images_abs),
            page_chunks=True,
        )
        results: list[PageResult] = []
        evidence: list[dict] = []
        for i, chunk in enumerate(chunks):
            meta = chunk.get("metadata") or {}
            page_no = int(meta.get("page_number") or i + 1)     # 1-indexed
            text = chunk.get("text") or ""
            results.append(PageResult(
                pages=(page_no,),
                source="text",
                markdown=normalize_image_refs(text, images_abs),
                backend=self.name,
            ))
            evidence.extend(page_evidence(text, page_no - 1))    # 证据层用 0-indexed
        results.sort(key=lambda r: r.page_no)

        # 页码注释由 merge_page_results 统一写出(格式只在这一处定义)
        md = merge_page_results(results)
        if self.bold_fonts:
            md = annotate_bold(md, pdf_path, extra_bold_fonts=self.bold_fonts)
        md = normalize_image_refs(md, images_abs)

        book_md = work_dir / "book.md"
        book_md.write_text(md, encoding="utf-8")
        write_local_evidence(work_dir, evidence,
                             backend=self.name, total_pages=len(results))
        img_count = len(list(images_abs.glob("*")))
        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            stats={"chars": len(md), "images": img_count, "pages": len(results)},
        )

    def page_results(
        self,
        pdf_path: str | Path,
        images_dir: str | Path,
        page_idxs: list[int],
    ) -> list[PageResult]:
        """只提取指定页(0-indexed)并**逐页**返回页单元(hybrid 流程用)。

        与 convert() 的差别:``page_chunks=True`` 让 PyMuPDF4LLM 按页返回,
        于是每页都能带着自己的页码进入 PageResult,交错页序不会错位。
        页码取自 chunk 元数据(1-indexed)而非入参顺序,再按页号排序 ——
        即使底层返回顺序变化,合并结果也不受影响。
        """
        pdf_path = Path(pdf_path)
        images_dir = Path(images_dir)
        images_dir.mkdir(parents=True, exist_ok=True)
        if not page_idxs:
            return []

        chunks = pymupdf4llm.to_markdown(
            str(pdf_path),
            pages=list(page_idxs),
            write_images=self.write_images,
            image_path=str(images_dir),
            page_chunks=True,
        )
        results: list[PageResult] = []
        for i, chunk in enumerate(chunks):
            meta = chunk.get("metadata") or {}
            page_no = int(meta.get("page_number") or i + 1)
            text = chunk.get("text") or ""
            results.append(PageResult(
                pages=(page_no,),
                source="text",
                markdown=normalize_image_refs(text, images_dir),
                backend=self.name,
            ))
        results.sort(key=lambda r: r.page_no)
        return results


# 兼容旧调用:convert_pdf_to_markdown(pdf, work) -> Path
def convert_pdf_to_markdown(
    pdf_path: str | Path,
    work_dir: str | Path,
    write_images: bool = True,
) -> Path:
    return PyMuPDFBackend(write_images=write_images).convert(pdf_path, work_dir).book_md
