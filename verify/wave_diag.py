# -*- coding: utf-8 -*-
"""检查光带颜色：有没有混进参考色板里没有的绿色。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from siri_orb import GlowRenderer

W, H = 1440, 900

def hue_of(rgb):
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.max(rgb, axis=-1); mn = np.min(rgb, axis=-1)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    idx = m & (mx == r)
    h[idx] = ((g[idx] - b[idx]) / d[idx]) % 6.0
    idx = m & (mx == g)
    h[idx] = ((b[idx] - r[idx]) / d[idx]) + 2.0
    idx = m & (mx == b)
    h[idx] = ((r[idx] - g[idx]) / d[idx]) + 4.0
    h = h / 6.0
    h[m] = np.mod(h[m], 1.0)
    return h

def main():
    gr = GlowRenderer(W, H)
    bw = max(40.0, W * 0.055)
    lines = []
    for frac in (0.12, 0.28, 0.5, 0.75):
        wave = {"radius": gr.max_r * frac, "width": bw}
        rgba = gr.render(12.0, 1.0, "listening", wave)
        im = Image.fromarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8)).resize((W, H), Image.LANCZOS)
        r = np.asarray(im).astype(np.float32) / 255.0
        a = r[..., 3]
        strong = a > 0.45
        px = r[..., :3][strong]
        if len(px) == 0:
            lines.append("frac=%.2f  no strong pixels" % frac); continue
        hh = hue_of(px)
        greenish = ((px[:, 1] > px[:, 0]) & (px[:, 1] > px[:, 2])).mean()
        hist, edges = np.histogram(hh, bins=12, range=(0, 1))
        lines.append("frac=%.2f  n=%d  meanRGB=(%.2f,%.2f,%.2f)  green_dominant=%.1f%%" % (
            frac, len(px), px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean(), greenish * 100))
        lines.append("   hue hist: " + " ".join("%.2f:%d" % (edges[i], hist[i]) for i in range(12) if hist[i]))
        Image.fromarray((np.clip(r, 0, 1) * 255).astype(np.uint8)).save(os.path.join(HERE, "band_%02d.png" % int(frac * 100)))
    with open(os.path.join(HERE, "wave_diag.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main()
