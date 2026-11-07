# -*- coding: utf-8 -*-
"""常驻实例实机复验：按钮亮 -> 波前扩散 -> 边光稳态 -> 顶部字幕卡。

纯定量，不靠肉眼。输出刻意全 ASCII（本机控制台是 GBK）。
用法： python tools\siri_orb\verify_live.py "tools\siri_orb\rv_0*.png"
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import glob
import re
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H = 1440, 900
ORIGIN = (0.9236 * W, 0.9711 * H)


def load(p):
    im = Image.open(p).convert("RGB")
    if im.size != (W, H):
        im = im.resize((W, H), Image.LANCZOS)
    return np.asarray(im).astype(np.float32) / 255.0


def masks():
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    card = np.zeros((H, W), bool)
    card[8:96, 360:1080] = True          # 顶部字幕卡
    bg = np.zeros((H, W), bool)
    bg[120:200, 360:1080] = True         # 卡片下方对照区
    d = np.sqrt((xx - ORIGIN[0]) ** 2 + (yy - ORIGIN[1]) ** 2)
    btn = d < 130                        # 按钮/波源附近
    edge = np.minimum(np.minimum(xx, W - 1 - xx),
                      np.minimum(yy, H - 1 - yy)) < 40
    return card, bg, btn, edge


def ascii_map(diff, cols=48, rows=18):
    """把变化量画成 ASCII 热力图（. : * #）。"""
    out = []
    bh, bw = diff.shape
    for r in range(rows):
        y0, y1 = int(r * bh / rows), int((r + 1) * bh / rows)
        row = []
        for c in range(cols):
            x0, x1 = int(c * bw / cols), int((c + 1) * bw / cols)
            v = float(diff[y0:y1, x0:x1].mean())
            row.append(" " if v < 0.01 else "." if v < 0.03 else
                       ":" if v < 0.08 else "*" if v < 0.20 else "#")
        out.append("".join(row))
    return out


def main():
    pat = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "rv_0*.png")
    # 排除 *_btn_*.png（按钮特写，尺寸跟整屏不一样，会污染统计）
    files = sorted([p for p in glob.glob(pat)
                    if re.search(r"_\d+\.png$", p) and "_btn_" not in os.path.basename(p)],
                   key=lambda p: int(re.search(r"_(\d+)\.png$", p).group(1)))
    if not files:
        print("no frames matched: %s" % pat)
        return 1
    card, bg, btn, edge = masks()
    print("frames=%d  size=%dx%d  origin=(%.0f,%.0f)" % (len(files), W, H, ORIGIN[0], ORIGIN[1]))
    print("%-16s %6s %7s %7s %6s %6s" % ("frame", "card", "card-bg", "button", "edge", "max"))
    out = []
    for p in files:
        lum = load(p).sum(axis=2) / 3.0
        c = float(lum[card].mean())
        b = float(lum[bg].mean())
        bt = float(lum[btn].max())
        e = float(lum[edge].mean())
        row = "%-16s %6.3f %+7.3f %7.3f %6.3f %6.3f" % (
            os.path.basename(p), c, c - b, bt, e, float(lum.max()))
        print(row)
        out.append(row)

    # 变化热力图：首帧(暗) vs 末帧(稳态)
    a0 = load(files[0])
    a1 = load(files[-1])
    diff = np.abs(a1 - a0).sum(axis=2) / 3.0
    ys, xs = np.nonzero(diff > 0.02)
    out.append("")
    out.append("=== change map: %s -> %s ===" % (os.path.basename(files[0]), os.path.basename(files[-1])))
    if len(xs):
        out.append("changed px(>0.02): %d  bbox x[%d..%d] y[%d..%d]"
                   % (len(xs), xs.min(), xs.max(), ys.min(), ys.max()))
    else:
        out.append("no pixel changed above 0.02")
    mp = ascii_map(diff)
    out.append("  " + "".join(str((i // 10) % 10) for i in range(48)))
    out.append("  " + "".join(str(i % 10) for i in range(48)))
    for i, r in enumerate(mp):
        out.append("%2d %s" % (i, r))
    print("")
    print("\n".join(out[-(len(mp) + 4):]))

    dst = os.path.join(HERE, "logs", "verify_live.txt")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
