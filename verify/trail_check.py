# -*- coding: utf-8 -*-
"""拖尾定量检查：波前扫过之后，亮度沿半径的分布。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from siri_orb import GlowRenderer

W, H = 1440, 900
gr = GlowRenderer(W, H, origin=(0.9236 * W, 0.9711 * H))
bw = max(30.0, W * 0.032)
db = gr.dbtn.ravel()

base_img = gr.render(0.0, 1.0, "listening", {"radius": -1.0e9, "width": bw})
base = base_img[..., 3].ravel()

print("base alpha: max=%.3f mean=%.3f" % (base.max(), base.mean()))
for R in (300.0, 700.0, 1100.0):
    a = gr.render(0.0, 1.0, "listening", {"radius": R, "width": bw})[..., 3].ravel()
    b = base
    print("-- wave R=%.0f --" % R)
    rows = []
    for lo in range(0, 1700, 100):
        m = (db >= lo) & (db < lo + 100)
        if m.sum() < 5:
            continue
        rows.append("%d:%+.3f" % (lo, float(a[m].mean() - b[m].mean())))
    print("   gain vs base, per 100px ring: %s" % " ".join(rows))
