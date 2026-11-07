# -*- coding: utf-8 -*-
"""定量分析实机帧：按到波源的距离分环，量每一环的亮度增益。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys, glob, re
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H = 1440, 900
ORIGIN = (0.9236 * W, 0.9711 * H)

def load(p):
    im = Image.open(p).convert("RGB").resize((W, H), Image.LANCZOS)
    return np.asarray(im).astype(np.float32)

def sat(x):
    mx = x.max(axis=2); mn = x.min(axis=2)
    return (mx - mn) / np.maximum(mx, 1.0)

def main():
    pat = sys.argv[1]
    files = sorted([p for p in glob.glob(pat) if re.search(r"_\d+\.png$", p)],
                   key=lambda p: int(re.search(r"_(\d+)\.png$", p).group(1)))
    if len(files) < 2:
        print("need >=2 frames"); return
    base = load(files[0])
    bs = sat(base)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt((xx - ORIGIN[0]) ** 2 + (yy - ORIGIN[1]) ** 2)
    edges = [0, 150, 300, 450, 600, 750, 900, 1100, 1300, 1600]
    lines = []
    lines.append("ring bins by distance from origin (px): %s" % edges)
    for p in files:
        f = load(p)
        g = (f.sum(axis=2) - base.sum(axis=2)) / 3.0 * sat(f)
        vals = []
        for i in range(len(edges) - 1):
            m = (d >= edges[i]) & (d < edges[i + 1])
            vals.append(float(g[m].mean()) if m.any() else 0.0)
        front = 0
        for i, v in enumerate(vals):
            if v > 1.5:
                front = edges[i + 1]
        lines.append("%-26s gain/ring=%s  front=%dpx" % (
            os.path.basename(p), " ".join("%5.1f" % v for v in vals), front))
    out = "\n".join(lines)
    with open(os.path.join(HERE, "frame_lit.txt"), "w", encoding="utf-8") as fh:
        fh.write(out)
    print(out)

if __name__ == "__main__":
    main()
