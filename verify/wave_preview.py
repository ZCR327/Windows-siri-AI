# -*- coding: utf-8 -*-
"""离线预览：把径向扩散的各个时刻渲染出来，贴在模拟壁纸上检查亮度。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from siri_orb import GlowRenderer

W, H = 1440, 900

def wallpaper(w, h):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 0.55 + 0.12 * np.sin(xx / 90.0) * np.cos(yy / 110.0)
    base = base + 0.18 * np.exp(-(((xx - w * 0.3) ** 2 + (yy - h * 0.7) ** 2) / (2 * 260.0 ** 2)))
    return np.stack([base * 1.0, base * 0.96, base * 0.90], axis=-1)

def main():
    gr = GlowRenderer(W, H)
    bg = wallpaper(W, H)
    tiles = []
    bw = max(40.0, W * 0.055)
    for frac in (0.0, 0.12, 0.28, 0.5, 0.75, 1.0):
        wave = None if frac <= 0 else {"radius": gr.max_r * frac, "width": bw}
        rgba = gr.render(12.0, 1.0, "listening", wave)
        im = Image.fromarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8))
        im = im.resize((W, H), Image.LANCZOS)
        r = np.asarray(im).astype(np.float32) / 255.0
        a = r[..., 3:4]
        comp = bg * (1 - a) + r[..., :3] * a
        tiles.append((frac, Image.fromarray((np.clip(comp, 0, 1) * 255).astype(np.uint8))))
    sheet = Image.new("RGB", (W // 2 * 3, H // 2 * 2), (0, 0, 0))
    for i, (frac, img) in enumerate(tiles):
        sheet.paste(img.resize((W // 2, H // 2), Image.LANCZOS),
                    ((i % 3) * (W // 2), (i // 3) * (H // 2)))
    sheet.save(os.path.join(HERE, "wave_sheet.png"))
    print("saved wave_sheet.png  origin=%s max_r=%.0f" % (gr.origin, gr.max_r))

if __name__ == "__main__":
    main()
