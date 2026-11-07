# -*- coding: utf-8 -*-
"""离线渲染边光预览：不开全屏层也能看拖尾/配色对不对。

用法：
    python preview.py out_prefix [t0 t1 t2 ...]
输出 out_prefix_0.png ... 每张是同一时刻的整屏边光（深色桌面底）。
"""
import os
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from siri_orb import GlowRenderer

W, H = 1440, 900
BG = (28, 30, 36)


def render(t, mode="listening"):
    gr = GlowRenderer(W, H)
    rgba = gr.render(t, 1.0, mode)
    bg = np.zeros((rgba.shape[0], rgba.shape[1], 3), dtype=np.float32)
    bg[:] = np.array(BG, dtype=np.float32) / 255.0
    a = rgba[..., 3:4]
    rgb = rgba[..., :3] * a + bg * (1.0 - a)
    im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))
    return im.resize((W, H), Image.LANCZOS)


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "preview")
    ts = [float(x) for x in sys.argv[2:]] or [0.0, 1.4, 2.6, 3.8, 5.0, 6.4]
    for i, t in enumerate(ts):
        im = render(t)
        path = "%s_%d.png" % (prefix, i)
        im.save(path)
        print("saved", path, "t=%.2f" % t)
        profile(t)


def profile(t, mode="listening"):
    """沿最外圈采样 alpha，打印亮度剖面（量化拖尾范围，比肉眼看靠谱）。"""
    gr = GlowRenderer(W, H)
    rgba = gr.render(t, 1.0, mode)
    a = rgba[..., 3]
    h, w = a.shape
    ring = np.concatenate([a[0, :], a[:, -1], a[-1, ::-1], a[::-1, 0]])
    n = len(ring)
    bins = 40
    prof = [ring[int(k * n / bins):int((k + 1) * n / bins)].mean() for k in range(bins)]
    lo = min(prof)
    hi = max(prof)
    # 亮于 (lo + 25%*(hi-lo)) 的部分算“波+拖尾”，其余算“底光”
    thr = lo + 0.25 * (hi - lo)
    active = sum(1 for v in prof if v > thr)
    bar = "".join("#" if v > thr else ("." if v > lo * 1.15 else " ") for v in prof)
    print("  profile t=%.2f lo=%.3f hi=%.3f thr=%.3f active=%d/%d (%.0f%%)"
          % (t, lo, hi, thr, active, bins, 100.0 * active / bins))
    print("  |%s|" % bar)


if __name__ == "__main__":
    main()
