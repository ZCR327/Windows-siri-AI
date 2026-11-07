# -*- coding: utf-8 -*-
"""验证「录声时边光按音量向内收缩」。

对同一时刻、不同电平各渲染一次，沿四条边的**法线方向**取剖面，
用最小二乘拟合  A*exp(-d/L) + C  求出边缘弥散特征长度 L（像素）。

判定用**比值**：实现里弥散长度 L_theory = 50*(1+0.95*level)，
实测拟合出的 L 会因为「四角热斑沿法线衰减」和跑马灯增益而比 L_theory 小一截
（固定比例），所以绝对值对不上是正常的 —— 只要
    L(level)/L(0)  ≈  1 + 0.95*level
就说明电平确实在驱动「往里钻」的深度。

注意：不能沿边线量「最远超过阈值的位置」——
  1) 沿边线量到的是整圈边光，不是钻入深度；
  2) 四角热斑是常数底噪，alpha 永远降不到阈值以下，会量成整屏。

用法： python tools\siri_orb\level_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

W, H = 1440, 900
LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]
FIT_LO, FIT_HI = 30.0, 120.0      # 拟合区间（逻辑像素）：避开白热核心与最外沿模糊
LS_GRID = np.arange(20.0, 220.0, 0.25)


def normal_profiles(alpha):
    """四条边沿法线向内的剖面（list of alpha 值，边->内）。"""
    lh, lw = alpha.shape
    n = int(FIT_HI / 3.0) + 2
    return {
        "top":    alpha[:n, lw // 2],
        "bottom": alpha[lh - 1 - n:, lw // 2][::-1],
        "left":   alpha[lh // 2, :n],
        "right":  alpha[lh // 2, lw - 1 - n:][::-1],
    }


def fit_length(profile, scale=3.0):
    """拟合 A*exp(-d/L)+C，返回 (L, A, C, r2)。"""
    d = np.arange(len(profile), dtype=np.float64) * scale
    m = (d >= FIT_LO) & (d <= FIT_HI)
    if m.sum() < 8:
        return None
    x, y = d[m], profile[m].astype(np.float64)
    best = None
    for L in LS_GRID:
        M = np.stack([np.exp(-x / L), np.ones_like(x)], axis=1)
        coef, _, _, _ = np.linalg.lstsq(M, y, rcond=None)
        sse = float(((y - M @ coef) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, float(L), float(coef[0]), float(coef[1]))
    sse, L, A, C = best
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - sse / ss_tot if ss_tot > 0 else 0.0
    return L, A, C, r2


def main():
    from siri_orb import GlowRenderer
    import cfgstore
    cfg = cfgstore.load()      # config.json 不在时自动用 DEFAULTS 兜底
    org = cfg.get("glow_origin", [0.9236, 0.9711])
    gr = GlowRenderer(W, H, origin=(org[0] * W, org[1] * H),
                      marquee_period=cfg.get("marquee_period_sec", 6.0),
                      marquee_trail=cfg.get("marquee_trail", 0.09))
    scale = float(W) / gr.lw
    print("grid %dx%d  scale=%.1f  origin=(%.0f, %.0f)"
          % (gr.lw, gr.lh, scale, org[0] * W, org[1] * H))
    print("")
    print("level   L(px)   visible3L(px)   A       C       r2     edges")
    rows, crops = [], []
    for lv in LEVELS:
        img = gr.render(1.0, 1.0, "listening", None, 1.0, lv)
        a = np.clip(img[..., 3], 0, 1)
        fits = {}
        for k, p in normal_profiles(a).items():
            f = fit_length(p, scale)
            if f:
                fits[k] = f
        Lm = float(np.mean([f[0] for f in fits.values()]))
        A = float(np.mean([f[1] for f in fits.values()]))
        C = float(np.mean([f[2] for f in fits.values()]))
        r2 = float(np.mean([f[3] for f in fits.values()]))
        print("%5.2f  %6.1f  %10.1f  %7.4f  %7.4f  %6.3f  %s"
              % (lv, Lm, 3.0 * Lm, A, C, r2,
                 " ".join("%s=%.0f" % (k, f[0]) for k, f in sorted(fits.items()))))
        rows.append((lv, Lm))
        crops.append((lv, img))

    L0 = rows[0][1]
    print("")
    print("level   measured L/L(0)   theory 1+0.95*level   rel.dev")
    devs = []
    for lv, L in rows:
        meas = L / L0
        theo = 1.0 + 0.95 * lv
        dev = abs(meas - theo) / theo
        devs.append(dev)
        print("%5.2f  %14.4f  %18.4f  %8.2f%%" % (lv, meas, theo, dev * 100))
    mono = all(rows[i + 1][1] > rows[i][1] + 0.5 for i in range(len(rows) - 1))
    maxdev = max(devs)
    print("")
    print("monotonic deeper with level: %s" % mono)
    print("depth range: %.1f px -> %.1f px  (x%.2f)" % (L0, rows[-1][1], rows[-1][1] / L0))
    print("max ratio deviation vs theory: %.2f%%" % (maxdev * 100))
    ok = mono and maxdev < 0.06
    print("[%s] level drives rim penetration" % ("PASS" if ok else "FAIL"))

    # ---- 拼图：上边中段裁剪，堆叠展示「往里钻」 ----
    cw, ch = 360, 96
    x0 = W // 2 - cw // 2
    cx0, cy0 = int(x0 / scale), 0
    ccw, cch = int(cw / scale), int(ch / scale)
    up = 3
    sheet = Image.new("RGB", (cw * up, (ch * up + 26) * len(crops)), (18, 20, 26))
    dr = ImageDraw.Draw(sheet)
    for i, (lv, img) in enumerate(crops):
        rgb = np.clip(img[..., :3], 0, 1)
        al = np.clip(img[..., 3:4], 0, 1)
        comp = al * rgb + (1.0 - al) * np.array([0.09, 0.10, 0.15], dtype=np.float32)
        crop = Image.fromarray((comp[cy0:cy0 + cch, cx0:cx0 + ccw] * 255).astype(np.uint8), "RGB")
        crop = crop.resize((cw * up, ch * up), Image.LANCZOS)
        y = i * (ch * up + 26)
        sheet.paste(crop, (0, y + 26))
        dr.text((6, y + 8), "level %.2f   L=%.0fpx   L/L0=%.3f   visible=%.0fpx"
                % (lv, rows[i][1], rows[i][1] / L0, 3.0 * rows[i][1]), fill=(220, 220, 230))
    out = os.path.join(HERE, "level_sheet.png")
    sheet.save(out)
    print("wrote %s" % out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
