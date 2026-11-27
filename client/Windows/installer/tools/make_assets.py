# make_assets.py —— 打包用的图标与界面字体
#
# 用法：python setup\tools\make_assets.py
# 产物（setup\out）：
#   BakerAI.ico        多尺寸图标（安装器 / 快捷方式 / 窗口共用），源 = 文档目录的 perlica2.png
#   cover.png          安装器左栏主视觉（同源，预裁一次，运行时按 2 倍图缩放）
#   BakerSairaField.ttf / BakerSairaLabel.ttf / BakerSairaDisplay.ttf
#                      Saira 可变字体切出的三个静态实例（wdth 112/125，wght 500/600/800）
#   Saira-OFL.txt      字体许可证（随包分发）

import os
import sys

from PIL import Image

SETUP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # setup\
ROOT = os.path.dirname(SETUP)                                              # 项目根
ICON_SRC = os.path.join(ROOT, 'baker-v3.2-文档', 'perlica2.png')
SAIRA_SRC = os.path.join(ROOT, 'endfield-ui', 'templates', 'flutter', 'fonts', 'Saira.ttf')
OFL_SRC = os.path.join(ROOT, 'endfield-ui', 'templates', 'flutter', 'fonts', 'Saira-OFL.txt')
OUT = os.path.join(SETUP, 'out')

ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

# 三个静态实例：(文件名, 家族名, wdth, wght)
FONTS = [
    ('BakerSairaField.ttf', 'Baker Saira Field', 112, 500),
    ('BakerSairaLabel.ttf', 'Baker Saira Label', 125, 600),
    ('BakerSairaDisplay.ttf', 'Baker Saira Display', 125, 800),
]

CHARS = (
    [chr(c) for c in range(0x20, 0x7F)]
    + ['·', '—', '…', '→', '│', '×', '≥', '≤', '‰', '№', '✓', '／', '：', '，', '。', '（', '）']
)


def crop_margin(img, ratio=0.02):
    """四周各裁 2%：原图留白略多，缩小后人物会显得太小"""
    w, h = img.size
    dx, dy = int(w * ratio), int(h * ratio)
    return img.crop((dx, dy, w - dx, h - dy))


def make_icon():
    img = Image.open(ICON_SRC).convert('RGBA')
    img = crop_margin(img)
    # 统一成正方形并缩到 256，Pillow 保存 ICO 时按 sizes 生成各档
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((256, 256), Image.LANCZOS)
    path = os.path.join(OUT, 'BakerAI.ico')
    img.save(path, format='ICO', sizes=[(s, s) for s in ICON_SIZES])
    print('icon  %s  %d B' % (path, os.path.getsize(path)))


def make_cover():
    """左栏主视觉：700×700，运行时缩到 284 逻辑像素（2 倍图）"""
    img = Image.open(ICON_SRC).convert('RGB')
    img = crop_margin(img, 0.01)
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((700, 700), Image.LANCZOS)
    path = os.path.join(OUT, 'cover.png')
    img.save(path, format='PNG', optimize=True)
    print('cover %s  %d B' % (path, os.path.getsize(path)))


def make_tabular(font):
    """Saira 默认是比例数字；GDI+ 不能选 OpenType 特性，这里把十个数字的步进改成一致
    （字形按差值居中平移），得到等宽数字。"""
    cmap = font.getBestCmap()
    names = [cmap[ord(c)] for c in '0123456789' if ord(c) in cmap]
    if len(names) != 10:
        return False
    target = max(font['hmtx'][g][0] for g in names)
    glyf = font['glyf']
    for g in names:
        adv, lsb = font['hmtx'][g]
        shift = (target - adv) / 2.0
        if shift:
            glyph = glyf[g]
            if glyph.numberOfContours != 0:
                glyph.coordinates.translate((shift, 0))
                if hasattr(glyph, 'xMin') and glyph.xMin is not None:
                    glyph.xMin = int(round(glyph.xMin + shift))
                    glyph.xMax = int(round(glyph.xMax + shift))
        font['hmtx'][g] = (target, int(round(lsb + shift)))
    return True


def make_fonts():
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont
    from fontTools import subset

    for fname, family, wdth, wght in FONTS:
        font = TTFont(SAIRA_SRC)
        instantiateVariableFont(font, {'wdth': wdth, 'wght': wght}, inplace=True)

        opts = subset.Options()
        opts.layout_features = ['*']
        opts.name_IDs = ['*']
        opts.notdef_outline = True
        sub = subset.Subsetter(options=opts)
        sub.populate(unicodes=[ord(c) for c in CHARS if ord(c) <= 0xFFFF])
        sub.subset(font)

        # 每个实例一个独立家族名：GDI+ 直接按名字取，不依赖字重匹配
        name = font['name']
        for rec in list(name.names):
            if rec.nameID in (1, 3, 4, 6, 16, 17, 21, 22):
                name.removeNames(nameID=rec.nameID)
        ps = family.replace(' ', '')
        name.setName(family, 1, 3, 1, 0x409)
        name.setName('Regular', 2, 3, 1, 0x409)
        name.setName(family + ' 2026', 3, 3, 1, 0x409)
        name.setName(family, 4, 3, 1, 0x409)
        name.setName(ps, 6, 3, 1, 0x409)
        font['OS/2'].usWeightClass = 400
        font['OS/2'].usWidthClass = 5

        tabular = make_tabular(font)

        path = os.path.join(OUT, fname)
        font.save(path)
        print('font  %s  %d B  wdth=%d wght=%d  tabular=%s' % (path, os.path.getsize(path), wdth, wght, tabular))


def copy_license():
    dst = os.path.join(OUT, 'Saira-OFL.txt')
    with open(OFL_SRC, 'rb') as f:
        data = f.read()
    with open(dst, 'wb') as f:
        f.write(data)
    print('ofl   %s  %d B' % (dst, len(data)))


if __name__ == '__main__':
    for p in (ICON_SRC, SAIRA_SRC, OFL_SRC):
        if not os.path.exists(p):
            print('missing:', p, file=sys.stderr)
            sys.exit(1)
    os.makedirs(OUT, exist_ok=True)
    make_icon()
    make_cover()
    make_fonts()
    copy_license()
    print('done')
