"""生成 Phase 3 测试 PDF(一次性辅助脚本)。

产物(samples/pdfs/):
- 中文电子书测试.pdf  单栏中文:标题层级 + 多段正文 + 2 张图片 + 页脚脚注 + 页码
- 双栏测试.pdf        双栏布局:左/右栏文本块交错插入(模拟真实双栏排版)
- 公式测试.tex        由 xelatex 编译,含行内/行间/多行公式(真实 LaTeX 渲染)

注意:本项目不依赖这些脚本运行;PDF 样本用于验证 PyMuPDF4LLM 提取质量。
"""
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "samples" / "pdfs"
OUT.mkdir(parents=True, exist_ok=True)
IMGS = Path(__file__).resolve().parent.parent / "samples" / "images"

FONT = "china-s"  # PyMuPDF 内置简体中文字体


def make_single_column() -> None:
    """单栏中文电子书:标题/章节/正文/图片/脚注/页码。"""
    doc = pymupdf.open()
    page_w, page_h = 595, 842  # A4
    margin = 60

    # ---- 第 1 页:书名 + 第一章 ----
    page = doc.new_page(width=page_w, height=page_h)
    # 书名
    page.insert_textbox(
        pymupdf.Rect(margin, 160, page_w - margin, 240),
        "中文电子书排版测试", fontname=FONT, fontsize=26, align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(margin, 245, page_w - margin, 280),
        "—— PyMuPDF4LLM 提取质量验证样本", fontname=FONT, fontsize=13,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )

    y = 330
    page.insert_textbox(
        pymupdf.Rect(margin, y, page_w - margin, y + 40),
        "第一章 引言", fontname=FONT, fontsize=18,
    )
    y += 50
    body1 = (
        "这是一本用于测试的电子书。它的文字层可以直接复制,属于电子版 PDF。"
        "本书由脚本生成,用于验证 PyMuPDF4LLM 从 PDF 提取中文正文、标题层级、"
        "图片和脚注的能力。正文中会混入英文术语,例如 Python、PDF、EPUB,"
        "以验证中英文混排的提取效果。\n\n"
        "第二段继续验证段落识别。中文书籍的正文排版通常采用两端对齐,"
        "段落之间通过空行或缩进区分。提取工具应当保留段落结构,"
        "而不是把整页文字压成一段。这里补充一些标点符号测试:逗号,句号。"
        "分号;冒号:括号(圆括号)【方括号】引号“双引号”和‘单引号’。"
    )
    page.insert_textbox(
        pymupdf.Rect(margin, y, page_w - margin, page_h - margin),
        body1, fontname=FONT, fontsize=11, lineheight=1.7,
    )
    # 页码
    page.insert_textbox(
        pymupdf.Rect(margin, page_h - 40, page_w - margin, page_h - 20),
        "第 1 页", fontname=FONT, fontsize=9, align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # ---- 第 2 页:第二章 图片 ----
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_textbox(
        pymupdf.Rect(margin, 60, page_w - margin, 100),
        "第二章 图片", fontname=FONT, fontsize=18,
    )
    page.insert_textbox(
        pymupdf.Rect(margin, 110, page_w - margin, 150),
        "本章插入两张图片,分别使用中文文件名和英文文件名,"
        "用于验证图片提取与命名处理。", fontname=FONT, fontsize=11, lineheight=1.6,
    )
    # 图片 1(中文文件名来源)
    img_rect1 = pymupdf.Rect(margin, 170, margin + 300, 170 + 197)
    page.insert_image(img_rect1, filename=str(IMGS / "测试插图.png"))
    page.insert_textbox(
        pymupdf.Rect(margin, 375, page_w - margin, 400),
        "图 1:中文文件名插图", fontname=FONT, fontsize=10, align=pymupdf.TEXT_ALIGN_CENTER,
    )
    # 图片 2
    img_rect2 = pymupdf.Rect(margin, 420, margin + 300, 420 + 200)
    page.insert_image(img_rect2, filename=str(IMGS / "figure1.png"))
    page.insert_textbox(
        pymupdf.Rect(margin, 628, page_w - margin, 650),
        "图 2:英文文件名插图", fontname=FONT, fontsize=10, align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # ---- 第 3 页:第三章 脚注 ----
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_textbox(
        pymupdf.Rect(margin, 60, page_w - margin, 100),
        "第三章 脚注", fontname=FONT, fontsize=18,
    )
    body3 = (
        "脚注在学术类书籍中十分常见[1]。本书用页脚位置的小号文字模拟脚注,"
        "用于验证提取工具是否能区分正文与脚注,并将脚注内容保留下来[2]。\n\n"
        "正文继续:脚注通常用上标数字标记,在页面底部给出注释内容。"
        "提取时如果能把脚注保留为可识别的结构,将有利于后续 EPUB 转换。"
    )
    page.insert_textbox(
        pymupdf.Rect(margin, 110, page_w - margin, 400),
        body3, fontname=FONT, fontsize=11, lineheight=1.7,
    )
    # 页底脚注(分隔线 + 小号字)
    page.draw_line(pymupdf.Point(margin, 700), pymupdf.Point(page_w - margin, 700), width=0.8)
    footnote = (
        "[1] 这是第一条脚注:脚注应当与正文区分,但内容不能丢失。\n"
        "[2] 第二条脚注:欧拉(Leonhard Euler,1707–1783)是瑞士数学家。"
    )
    page.insert_textbox(
        pymupdf.Rect(margin, 710, page_w - margin, 800),
        footnote, fontname=FONT, fontsize=8.5, lineheight=1.5,
    )
    page.insert_textbox(
        pymupdf.Rect(margin, page_h - 40, page_w - margin, page_h - 20),
        "第 3 页", fontname=FONT, fontsize=9, align=pymupdf.TEXT_ALIGN_CENTER,
    )

    path = OUT / "中文电子书测试.pdf"
    doc.save(path)
    print(f"OK: {path} ({doc.page_count} 页)")


def make_two_column() -> None:
    """双栏布局:左右栏文本块交错插入,模拟真实双栏排版的文本流。"""
    doc = pymupdf.open()
    page_w, page_h = 595, 842
    margin = 55
    gutter = 30
    col_w = (page_w - 2 * margin - gutter) / 2

    def col_rect(left: bool, y0: float, y1: float) -> pymupdf.Rect:
        x0 = margin if left else margin + col_w + gutter
        return pymupdf.Rect(x0, y0, x0 + col_w, y1)

    # 跨栏标题
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_textbox(
        pymupdf.Rect(margin, 70, page_w - margin, 110),
        "双栏排版测试", fontname=FONT, fontsize=20, align=pymupdf.TEXT_ALIGN_CENTER,
    )

    # 左右栏交替的文本块(模拟物理排版的阅读顺序流)
    left_chunks = [
        "双栏排版常见于学术期刊和杂志。左栏第一段内容,介绍双栏排版的背景。",
        "左栏第二段:继续讨论双栏与单栏的差异,以及阅读时的视线移动规律。",
        "左栏第三段:双栏提取的难点在于阅读顺序的重组,需要按栏正确还原。",
        "左栏第四段:工具应当先识别栏结构,再按从左到右、从上到下组织段落。",
    ]
    right_chunks = [
        "右栏第一段:右栏与左栏并行排版,内容与左栏相互呼应。",
        "右栏第二段:双栏中的标题通常跨栏居中,正文才分栏排布。",
        "右栏第三段:提取结果应保持左右栏各自的段落顺序,不能交错混乱。",
        "右栏第四段:如果提取后左右栏文字混在一起,将严重损害可读性。",
    ]

    y = 150
    block_h = 70
    for left, right in zip(left_chunks, right_chunks):
        # 先左后右(物理排版顺序)
        page.insert_textbox(col_rect(True, y, y + block_h), left, fontname=FONT, fontsize=10.5, lineheight=1.6)
        page.insert_textbox(col_rect(False, y, y + block_h), right, fontname=FONT, fontsize=10.5, lineheight=1.6)
        y += block_h

    # 第二页继续
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_textbox(
        pymupdf.Rect(margin, 70, page_w - margin, 110),
        "双栏排版测试(续)", fontname=FONT, fontsize=16, align=pymupdf.TEXT_ALIGN_CENTER,
    )
    left2 = [
        "续页左栏第一段:跨页时双栏文本的连续性也是提取难点之一。",
        "续页左栏第二段:跨栏段落需要拼接,不能断成两截。",
        "续页左栏第三段:好的提取结果应当与视觉阅读顺序一致。",
    ]
    right2 = [
        "续页右栏第一段:本文用于验证双栏提取的段落顺序保持。",
        "续页右栏第二段:检查点:左栏在上,右栏在下,顺序不乱。",
        "续页右栏第三段:同时验证多页双栏内容的连续提取。",
    ]
    y = 130
    for left, right in zip(left2, right2):
        page.insert_textbox(col_rect(True, y, y + 70), left, fontname=FONT, fontsize=10.5, lineheight=1.6)
        page.insert_textbox(col_rect(False, y, y + 70), right, fontname=FONT, fontsize=10.5, lineheight=1.6)
        y += 70

    path = OUT / "双栏测试.pdf"
    doc.save(path)
    print(f"OK: {path} ({doc.page_count} 页)")


def write_math_tex() -> None:
    """写 xelatex 源文件:含行内/行间/多行公式,尽量加入中文文本。"""
    tex = r"""\documentclass[11pt]{article}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{ctex}
\usepackage[a4paper,margin=2.5cm]{geometry}
\title{数学公式测试文档}
\author{pdf2epub 测试}
\date{}
\begin{document}
\maketitle
\section{行内公式}
质能方程 $E = mc^2$ 是最著名的行内公式。欧拉恒等式 $e^{i\pi}+1=0$
出现在句子中间。化学式 $H_2O$ 与下标 $x_1, x_2, \ldots, x_n$ 也是常见用例。

\section{行间公式}
下面是一段独立的行间公式:
\[
\int_{-\infty}^{\infty} e^{-x^2}\,dx = \sqrt{\pi}
\]
分数与根号:
\[
\frac{a+b}{a-b} = \frac{\sqrt{x^2+y^2}}{z}
\]

\section{多行公式}
\begin{equation}
\begin{aligned}
(a+b)^2 &= a^2 + 2ab + b^2 \\
(a-b)^2 &= a^2 - 2ab + b^2
\end{aligned}
\end{equation}

\begin{equation}
\sum_{k=1}^{n} k = \frac{n(n+1)}{2}, \qquad
\lim_{x \to 0} \frac{\sin x}{x} = 1
\end{equation}

\section{混合段落}
公式与中文混排的段落用于验证提取时公式块的定位与保留。行间公式应当独立成块,
不能被拆分进相邻段落。
\end{document}
"""
    path = OUT / "公式测试.tex"
    path.write_text(tex, encoding="utf-8")
    print(f"OK: {path}")


if __name__ == "__main__":
    make_single_column()
    make_two_column()
    write_math_tex()
