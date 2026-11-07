# -*- coding: utf-8 -*-
"""配色 / 拖尾 自查：把渲染结果沿 theta 采样，和参考视频实测表并排对。

    python tools\siri_orb\palette_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import json
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from siri_orb import GlowRenderer

W, H = 1440, 900


def composite(img, bg):
    rgb = np.clip(img[..., :3], 0, 1)
    a = np.clip(img[..., 3:4], 0, 1)
    return bg * (1 - a) + rgb * a


def sample_rim(img, nsec=24):
    """沿 theta 采样边缘一圈的 (hue, sat, val)。"""
    import colorsys
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx - (w - 1) / 2.0) / max(1.0, (w - 1) / 2.0)
    ny = (yy - (h - 1) / 2.0) / max(1.0, (h - 1) / 2.0)
    th = np.mod(np.arctan2(ny, nx) / (2.0 * np.pi), 1.0)
    edge = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy))
    band = (edge > 18) & (edge < 46)   # 跳过最内圈的白热核心层
    rows = []
    for k in range(nsec):
        m = band & (th >= k / nsec) & (th < (k + 1) / nsec)
        if m.sum() < 20:
            rows.append((k, None, None, None))
            continue
        sel = img[m]
        a = sel[:, 3]
        strong = sel[a > np.percentile(a, 70)]
        if strong.shape[0] < 10:
            strong = sel
        r, g, b = np.median(strong[:, :3], axis=0)
        hh, ss, vv = colorsys.rgb_to_hsv(float(r), float(g), float(b))
        rows.append((k, round(hh, 3), round(ss, 3), round(vv, 3)))
    return rows


def main():
    cfg = {}
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    go = cfg.get("glow_origin") or [0.9236, 0.9711]
    gr = GlowRenderer(W, H, origin=(float(go[0]) * W, float(go[1]) * H))
    bw = max(30.0, W * float(cfg.get("wave_band_frac", 0.032)))
    delay = float(cfg.get("wave_delay_sec", 0.2))
    sweep = float(cfg.get("wave_sweep_sec", 0.5))

    from PIL import Image
    bg = np.zeros((gr.lh, gr.lw, 3), dtype=np.float32)
    yy = np.linspace(0, 1, gr.lh, dtype=np.float32)[:, None]
    bg[..., 0] = 0.10 + 0.05 * yy
    bg[..., 1] = 0.11 + 0.05 * yy
    bg[..., 2] = 0.16

    # 1) 稳态整圈
    img = gr.render(3.0, 1.0, "listening", None, 1.0)
    comp = composite(img, bg)
    Image.fromarray((comp * 255).astype(np.uint8)).resize((W, H), Image.LANCZOS).save(
        os.path.join(HERE, "chk_steady.png"))

    print("=== spline value by theta (what the palette is supposed to be) ===")
    import colorsys as _cs
    for k in range(24):
        th = (k + 0.5) / 24.0
        hh, ss = gr._palette(np.array([th]))
        r, g, b = _cs.hsv_to_rgb(float(hh[0]) % 1.0, float(np.clip(ss[0], 0, 1)), 1.0)
        print("  theta=%.3f hue=%.3f sat=%.3f" % (th, float(hh[0]) % 1.0, float(ss[0])))

    print("=== steady rim pixels, sampled by theta (0=right 0.25=bottom 0.5=left 0.75=top) ===")
    for k, hh, ss, vv in sample_rim(img):
        if hh is None:
            continue
        print("  theta=%.3f hue=%.3f sat=%.3f val=%.3f" % ((k + 0.5) / 24.0, hh, ss, vv))

    # 2) 扩散序列
    ts = [0.00, 0.10, 0.20, 0.26, 0.34, 0.44, 0.54, 0.70]
    cols, rows = 4, 2
    sheet = Image.new("RGB", (gr.lw * cols, gr.lh * rows), (26, 28, 38))
    for i, t in enumerate(ts):
        if t < delay:
            wave = {"radius": -1.0e9, "width": bw}
        elif t < delay + sweep:
            tau = (t - delay) / max(1e-3, sweep)
            wave = {"radius": gr.max_r * (1.0 - (1.0 - tau) ** 1.7), "width": bw}
        else:
            wave = None
        sh = 0.0 if wave is not None else min(1.0, max(0.0, (t - delay - sweep) / 0.5))
        im2 = gr.render(t, 1.0, "listening", wave, sh)
        c2 = composite(im2, bg)
        sheet.paste(Image.fromarray((c2 * 255).astype(np.uint8)),
                    ((i % cols) * gr.lw, (i // cols) * gr.lh))
    sheet.save(os.path.join(HERE, "chk_expand.png"))
    print("wrote chk_steady.png / chk_expand.png")


if __name__ == "__main__":
    main()
