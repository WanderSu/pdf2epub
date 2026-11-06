"""封面页识别与 cover.jpg 生成。

两件事必须分开看,混在一起就会两头不讨好:

1. **这一页是不是封面**(``detect_cover``)—— 决定 EPUB 要不要生成标题页。
   封面本来就写着书名/作者/出版社,再让 pandoc 生成一张"标题 + 作者 + 日期"的
   标题页,读者打开书会先看到两遍同样的信息。
2. **cover.jpg 怎么取**(``render_cover``)—— 优先用原书自带的封面图,而不是把整页
   重新栅格化:扫描件的整页渲染会连扫描仪的黑边、纸张底色、阴影一起拍进去,而原图
   本身就是出版社排好的那张封面。

判据全部可数,不做语义猜测:

- ``image_ratio``:页面里最大一张图占页面面积的比例。扫描封面 ≈ 1.0,正文页里的
  插图通常 < 0.5;
- ``text_chars``:该页文字层去空白后的字符数。封面通常 ≤ 几十个字(书名 + 作者);
- 空白页(0 字且无图)不算封面 —— 拿它当 cover.jpg 只会得到一张白纸。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: cover.jpg 的目标宽度(与原 COVER_WIDTH 一致:1200px 足够阅读器缩略图与全屏)
COVER_WIDTH = 1200
#: cover.jpg 的 JPEG 质量
COVER_QUALITY = 88

#: 一张图占页面面积超过这个比例 → 这一页以图为主(扫描封面/整页插图)
IMAGE_DOMINANT_RATIO = 0.6
#: 文字层字符数不超过这个值,且页面里有图 → 判定为封面
COVER_MAX_CHARS = 80
#: 没有图时(纯文字排版的封面/扉页):文字上限、文字块上限
COVER_MAX_CHARS_NO_IMAGE = 120
COVER_MAX_BLOCKS_NO_IMAGE = 6

_WS_RE = re.compile(r"\s+")
#: 句末标点:真封面上的书名/作者/社名不会以它收尾,正文段落会
_SENTENCE_END = tuple("。!?…")


@dataclass(frozen=True)
class CoverInfo:
    """第 1 页的封面判定结果(可序列化,进事件/日志用)。"""

    page_index: int = 0
    is_cover: bool = False
    image_ratio: float = 0.0      # 最大图片占页面面积比例
    text_chars: int = 0           # 文字层非空白字符数
    native_width: int = 0         # 主图原始像素宽(0 = 没有主图)
    clip: tuple[float, float, float, float] | None = None  # 主图矩形(裁掉扫描黑边)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "page": self.page_index + 1,
            "is_cover": self.is_cover,
            "image_ratio": round(self.image_ratio, 3),
            "text_chars": self.text_chars,
            "native_width": self.native_width,
            "reason": self.reason,
        }


def _largest_image(page) -> tuple[float, int, tuple[float, float, float, float] | None]:
    """页面上最大的一张图:(面积占比, 原始像素宽, 矩形)。

    一个图可能被同一页引用多次(水印、重复贴图),矩形取并集会算得偏大,所以按
    **单次放置**取最大面积,不做并集。
    """
    page_area = abs(page.rect.width * page.rect.height) or 1.0
    best = (0.0, 0, None)
    try:
        images = page.get_images(full=True)
    except Exception:                                   # noqa: BLE001 - 取不到就当没有图
        return best
    for item in images:
        xref = item[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:                               # noqa: BLE001
            continue
        for rect in rects:
            area = abs(rect.width * rect.height)
            ratio = area / page_area
            if ratio > best[0]:
                native = int(item[2] or 0)              # (xref, smask, width, height, …)
                best = (ratio, native, (rect.x0, rect.y0, rect.x1, rect.y1))
    return best


def _page_blocks(page) -> list[str]:
    """页面的文字块(去掉页码/空白块)。封面只有几块,正文页多且长。"""
    try:
        blocks = page.get_text("blocks") or []
    except Exception:                                   # noqa: BLE001
        return []
    out = []
    for block in blocks:
        text = (block[4] or "").strip() if len(block) > 4 else ""
        if text:
            out.append(text)
    return out


def detect_cover(pdf_path: str | Path, page_index: int = 0) -> CoverInfo:
    """判定第 ``page_index`` 页(0-indexed)是不是封面。读不出来就当"不是"。"""
    try:
        import pymupdf
    except Exception:                                   # noqa: BLE001 - 缺依赖不影响主链路
        return CoverInfo(page_index=page_index, reason="pymupdf 不可用")
    try:
        with pymupdf.open(pdf_path) as doc:
            if doc.page_count == 0 or page_index >= doc.page_count:
                return CoverInfo(page_index=page_index, reason="没有这一页")
            page = doc[page_index]
            text_chars = len(_WS_RE.sub("", page.get_text("text") or ""))
            ratio, native, rect = _largest_image(page)
            blocks = _page_blocks(page)
    except Exception as exc:                            # noqa: BLE001 - 封面判定不该阻断转换
        return CoverInfo(page_index=page_index, reason=f"读取失败: {exc}")

    if ratio >= IMAGE_DOMINANT_RATIO:
        return CoverInfo(page_index, True, ratio, text_chars, native, rect,
                         f"整页图占 {ratio:.0%}")
    if ratio > 0 and text_chars <= COVER_MAX_CHARS:
        return CoverInfo(page_index, True, ratio, text_chars, native, rect,
                         f"有图且文字仅 {text_chars} 字")
    if ratio == 0 and 3 <= text_chars <= COVER_MAX_CHARS_NO_IMAGE:
        # 纯文字封面/扉页(出版社排版的书名页):几块短字段,没有正文段落。
        # 「有没有句末标点」是这里唯一的硬判据 —— 正文段落一定以句号收尾。
        prose = any(b.rstrip().endswith(_SENTENCE_END) for b in blocks)
        if not prose and len(blocks) <= COVER_MAX_BLOCKS_NO_IMAGE:
            return CoverInfo(page_index, True, ratio, text_chars, 0, None,
                             f"无图,{len(blocks)} 块短文字({text_chars} 字)")
    if text_chars == 0 and ratio == 0:
        return CoverInfo(page_index, False, ratio, text_chars, 0, None, "空白页")
    return CoverInfo(page_index, False, ratio, text_chars, native, rect,
                     f"文字 {text_chars} 字,图占 {ratio:.0%}")


def render_cover(pdf_path: str | Path, out: str | Path, *,
                 info: CoverInfo | None = None,
                 width: int = COVER_WIDTH, quality: int = COVER_QUALITY) -> Path | None:
    """把封面页写成 ``<out>``(JPEG),返回路径;失败返回 None(不影响正文转换)。

    - 有主图(占页面 ≥60%)时按**主图矩形**裁切:扫描件整页渲染会把扫描仪黑边和
      纸张底色一起拍进去,裁到内容区就干净了;
    - 缩放上限取「目标宽度」与「原图原生分辨率」中的较小者:原图只有 600px 宽时
      放大到 1200px 只会变糊,不解决问题。
    """
    try:
        import pymupdf

        info = info or detect_cover(pdf_path)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf_path) as doc:
            if doc.page_count == 0 or info.page_index >= doc.page_count:
                return None
            page = doc[info.page_index]
            clip = pymupdf.Rect(info.clip) if info.clip else page.rect
            base = max(1.0, clip.width)
            zoom = width / base
            if info.native_width:
                zoom = min(zoom, max(0.2, info.native_width / base))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
            pix.save(str(out), jpg_quality=quality)
        return out if out.exists() and out.stat().st_size else None
    except Exception:                                   # noqa: BLE001 - 封面失败不影响正文
        return None


__all__ = ["COVER_WIDTH", "COVER_QUALITY", "CoverInfo", "detect_cover", "render_cover"]
