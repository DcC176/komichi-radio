# -*- coding: utf-8 -*-
"""生成站点图标 favicon.ico。

一个文件同时服务两处：浏览器标签页图标，以及打包出来的 EXE 图标。
配色取自 assets/style.css 的 --panel / --red。

用法：
    python tools/make_icon.py
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "favicon.ico")

SIZE = 256
BG = (16, 16, 19, 255)            # --panel  #101013
RED = (255, 59, 78, 255)          # --red    #ff3b4e
EDGE = (255, 255, 255, 30)        # 极淡描边：深色底在深色任务栏上也能看出边界


def make():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([1, 1, SIZE - 2, SIZE - 2], radius=58,
                        fill=BG, outline=EDGE, width=3)

    font = ImageFont.truetype("arialbd.ttf", 130)
    d.text((SIZE / 2, SIZE / 2 - 16), "24", font=font, fill=RED, anchor="mm")

    # 底部一道红线：电台信号条的暗示
    d.rounded_rectangle([SIZE * 0.30, SIZE * 0.80, SIZE * 0.70, SIZE * 0.80 + 13],
                        radius=7, fill=RED)
    return img


def main():
    img = make()
    img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                         (128, 128), (256, 256)])
    print("已生成 %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
