# -*- coding: utf-8 -*-
"""在真实桌面截图上离线合成径向扩散的各个阶段。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys, time
import numpy as np
from PIL import Image, ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from siri_orb import GlowRenderer

W, H = 1440, 900

def main():
    time.sleep(0.4)
    bg_im = ImageGrab.grab().resize((W, H), Image.LANCZOS)
    bg_im.save(os.path.join(HERE, "desktop_clean.png"))
    bg = np.asarray(bg_im).astype(np.float32) / 255.0
    gr = GlowRenderer(W, H)
    bw = max(40.0, W * 0.055)
    tiles = []
    for frac in (0.0, 0.10, 0.22, 0.38, 0.58, 0.80, 1.0):
        wave = None if frac <= 0 else {"radius": gr.max_r * frac, "width": bw}
        rgba = gr.render(12.0, 1.0, "listening", wave)
        im = Image.fromarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8)).resize((W, H), Image.LANCZOS)
        r = np.asarray(im).astype(np.float32) / 255.0
        a = r[..., 3:4]
        comp = bg * (1 - a) + r[..., :3] * a
        tiles.append(Image.fromarray((np.clip(comp, 0, 1) * 255).astype(np.uint8)))
    sheet = Image.new("RGB", (W // 2 * 4, H // 2 * 2), (10, 10, 12))
    for i, img in enumerate(tiles):
        sheet.paste(img.resize((W // 2, H // 2), Image.LANCZOS),
                    ((i % 4) * (W // 2), (i // 4) * (H // 2)))
    sheet.save(os.path.join(HERE, "wave_desktop_sheet.png"))
    print("saved wave_desktop_sheet.png  origin=%s max_r=%.0f" % (gr.origin, gr.max_r))

if __name__ == "__main__":
    main()
