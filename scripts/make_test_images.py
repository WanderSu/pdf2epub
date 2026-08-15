"""生成 Phase 2 测试图片(一次性辅助脚本,非项目运行依赖)。

- samples/images/测试插图.png  中文文件名 + 中文文字(验证中文文件名处理)
- samples/images/figure1.png   常规英文名图形
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples" / "images"
OUT.mkdir(parents=True, exist_ok=True)

# Windows 常见中文字体,按可用性回退
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",    # 微软雅黑
    r"C:\Windows\Fonts\simhei.ttf",  # 黑体
    r"C:\Windows\Fonts\simsun.ttc",  # 宋体
]


def load_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def make_chinese_image() -> None:
    """中文文件名测试图:蓝底 + 中文文字 + 色块。"""
    w, h = 640, 420
    img = Image.new("RGB", (w, h), "#2b5aa6")
    draw = ImageDraw.Draw(img)

    # 装饰色块
    draw.rectangle([40, 40, 600, 120], fill="#e8a33d")
    draw.rectangle([40, 140, 600, 380], fill="#f5f5f5")

    font = load_font(48)
    if font:
        draw.text((70, 60), "中文测试插图", fill="#1a1a1a", font=font)
        draw.text((70, 220), "PDF → EPUB 项目", fill="#2b5aa6", font=load_font(36))
    else:
        draw.text((70, 60), "No CJK font", fill="#1a1a1a")

    img.save(OUT / "测试插图.png", "PNG")
    print(f"OK: {OUT / '测试插图.png'} ({w}x{h})")


def make_figure() -> None:
    """英文名图形:渐变方块 + 圆形。"""
    w, h = 480, 320
    img = Image.new("RGB", (w, h), "#ffffff")
    draw = ImageDraw.Draw(img)

    for i in range(6):
        x0 = 30 + i * 72
        shade = 40 + i * 35
        draw.rectangle([x0, 40, x0 + 56, 160], fill=(shade, shade + 30, 220 - i * 25))

    draw.ellipse([160, 190, 320, 280], fill="#e8a33d", outline="#8a5a00", width=3)
    draw.line([30, 280, 450, 280], fill="#888", width=2)

    img.save(OUT / "figure1.png", "PNG")
    print(f"OK: {OUT / 'figure1.png'} ({w}x{h})")


if __name__ == "__main__":
    make_chinese_image()
    make_figure()
