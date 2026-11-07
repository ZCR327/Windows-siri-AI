# -*- coding: utf-8 -*-
"""数值诊断：波头位置 + 拖尾实际长度（周长占比）。

沿最外圈采样 alpha，剔除四角固定热斑的干扰后，
找出「亮带起点=波头」和「亮带终点=拖尾尾端」。
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from siri_orb import GlowRenderer

W, H = 1440, 900


def ring_samples(t, mode="listening"):
    gr = GlowRenderer(W, H)
    rgba = gr.render(t, 1.0, mode)
    a = rgba[..., 3]
    h, w = a.shape
    # 最外圈一圈（顺时针：上边→右边→下边→左边）
    ring = np.concatenate([a[0, :], a[:, -1], a[-1, ::-1], a[::-1, 0]])
    n = len(ring)
    # 环上每个位置对应的 theta（0=右，顺时针；图像坐标 y 向下，所以底边 theta≈0.25）
    idx = np.arange(n)
    # 上边从左到右：theta 0.75→1.25(=0.25)
    seg = np.concatenate([
        np.linspace(0.75, 1.25, w, endpoint=False),                 # 上边
        np.linspace(-0.25, 0.25, h, endpoint=False),                # 右边
        np.linspace(0.25, -0.25, w, endpoint=False),                # 下边（反向）
        np.linspace(0.25, 0.75, h, endpoint=False),                 # 左边
    ])
    theta = np.mod(seg, 1.0)
    return ring, theta


def analyze(t, mode="listening"):
    ring, theta = ring_samples(t, mode)
    n = len(ring)
    lo = np.percentile(ring, 10)
    hi = ring.max()
    thr = lo + 0.45 * (hi - lo)
    active = ring > thr
    if not active.any():
        return None
    # 找最长连续亮带（拖尾方向 = theta 递增方向）
    best_len = 0
    best = None
    start = None
    for i in range(n + 1):
        if i < n and active[i]:
            if start is None:
                start = i
        else:
            if start is not None:
                L = i - start
                if L > best_len:
                    best_len = L
                    best = (start, i)
                start = None
    s, e = best
    head_theta = theta[s]
    tail_theta = theta[(e - 1) % n]
    # 沿 theta 递增方向的弧长占比
    span = (tail_theta - head_theta) % 1.0
    print("t=%5.2f  lo=%.3f hi=%.3f thr=%.3f" % (t, lo, hi, thr))
    print("   亮带长度 = %d/%d 环周 (%.0f%%)   波头 theta=%.3f  尾端 theta=%.3f"
          % (best_len, n, 100.0 * best_len / n, head_theta, tail_theta))
    return best_len / n


if __name__ == "__main__":
    ts = [float(x) for x in sys.argv[1:]] or [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    lens = []
    for t in ts:
        v = analyze(t)
        if v:
            lens.append(v)
    if lens:
        print("\n拖尾长度汇总：平均 %.0f%%  最大 %.0f%%  最小 %.0f%%"
              % (100 * np.mean(lens), 100 * max(lens), 100 * min(lens)))
