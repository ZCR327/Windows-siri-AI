# -*- coding: utf-8 -*-
"""把实机截图的四周边缘裁出来拼成一张对照图，方便验收边光。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def crop_ring(path, band=90):
    im = Image.open(path)
    w, h = im.size
    top = im.crop((0, 0, w, band))
    right = im.crop((w - band, 0, w, h)).rotate(-90, expand=True)
    bottom = im.crop((0, h - band, w, h)).rotate(180, expand=True)
    left = im.crop((0, 0, band, h)).rotate(90, expand=True)
    W = w
    H = band * 4
    out = Image.new("RGB", (W, H), (16, 17, 20))
    out.paste(top, (0, 0))
    out.paste(right, (W - h, band))
    out.paste(bottom, (0, band * 2))
    out.paste(left, (0, band * 3))
    return out


if __name__ == "__main__":
    for f in sys.argv[1:]:
        src = os.path.join(HERE, f) if not os.path.isabs(f) else f
        dst = os.path.splitext(src)[0] + "_ring.png"
        crop_ring(src).save(dst)
        print("saved", dst)
