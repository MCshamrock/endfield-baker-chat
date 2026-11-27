# make_brand_assets.py —— 生成 Baker AI 4 的图标与安装器主视觉
#
# 源图：frontend-src/v322kit/perlica2.png（与 3.2.2 同一张，品牌延续）
# 产物（Windows/installer/out）：
#   BakerAI.ico   多尺寸图标，右下角加一个「04」黑底黄字角标，和旧版一眼区分
#   cover.png     安装器左栏主视觉（700×700，运行时按 2 倍图缩放）
#
# 字体用同一目录里已经切好的 BakerSairaDisplay.ttf（Saira 125 宽 800 重）。

import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SETUP))          # client
SRC = os.path.join(ROOT, '..', '..', 'frontend-src', 'v322kit', 'perlica2.png')
OUT = os.path.join(SETUP, 'out')
FONT = os.path.join(OUT, 'BakerSairaDisplay.ttf')
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


def square(img, margin_ratio):
    """裁掉四周留白，再居中裁成正方形（不拉伸人物）"""
    w, h = img.size
    dx, dy = int(w * margin_ratio), int(h * margin_ratio)
    img = img.crop((dx, dy, w - dx, h - dy))
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    return img.crop((left, top, left + side, top + side))


def draw_version_badge(img):
    """右下角黑底黄字「04」：切角沿用界面规则，数字是真的版本号"""
    size = img.size[0]
    s = size / 512.0
    box = int(116 * s)          # 角标边长
    cut = int(20 * s)           # 右下切角
    x0 = size - box
    y0 = size - box

    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon(
        [
            (x0, y0),
            (size - 1, y0),
            (size - 1, size - 1 - cut),
            (size - 1 - cut, size - 1),
            (x0, size - 1),
        ],
        fill=(22, 22, 22, 255),
    )
    font = ImageFont.truetype(FONT, int(92 * s))
    text = '4'
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + (box - cut / 2 - tw) / 2
    ty = y0 + (box - cut / 2 - th) / 2 - bbox[1]
    d.text((tx, ty), text, font=font, fill=(255, 250, 0, 255))
    return Image.alpha_composite(img.convert('RGBA'), layer)


def make_icon():
    img = square(Image.open(SRC).convert('RGBA'), 0.02)
    img = draw_version_badge(img.resize((512, 512), Image.LANCZOS))
    path = os.path.join(OUT, 'BakerAI.ico')
    img.save(path, format='ICO', sizes=[(n, n) for n in ICON_SIZES])
    print('icon  %s  %d B' % (path, os.path.getsize(path)))


def make_cover():
    img = square(Image.open(SRC).convert('RGB'), 0.01)
    img = img.resize((700, 700), Image.LANCZOS)
    path = os.path.join(OUT, 'cover.png')
    img.save(path, format='PNG', optimize=True)
    print('cover %s  %d B' % (path, os.path.getsize(path)))


if __name__ == '__main__':
    if not os.path.exists(SRC):
        raise SystemExit('找不到源图: %s' % SRC)
    if not os.path.exists(FONT):
        raise SystemExit('找不到字体: %s' % FONT)
    make_icon()
    make_cover()
