# -*- coding: utf-8 -*-
"""量实机边缘亮度：外圈若干像素带的平均 RGB/alpha。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rim_report(path, label):
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.float32) / 255.0
    h, w = a.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    edge = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy))
    print("%-22s %s  size=%dx%d" % (label, os.path.basename(path), w, h))
    for lo, hi in ((0, 6), (6, 14), (14, 28), (28, 50), (50, 90)):
        m = (edge >= lo) & (edge < hi)
        if m.sum() < 50:
            continue
        px = a[m]
        print("   edge %3d-%3dpx  n=%-8d mean=(%.3f,%.3f,%.3f) max=%d" % (
            lo, hi, m.sum(), px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean(),
            int(255 * px.max())))


for name, label in (("nv_02.png", "old white-boost 0.45"),
                    ("fx_03.png", "new white-boost 0.26"),
                    ("fx_00.png", "mid-expansion")):
    p = os.path.join(HERE, name)
    if os.path.exists(p):
        rim_report(p, label)
