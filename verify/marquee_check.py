# -*- coding: utf-8 -*-
"""跑马灯验证：亮头在绕屏幕边缘跑吗？尾巴有多长？

两种模式：
    python tools\siri_orb\marquee_check.py offline        # 离线渲染：追亮头角度 + 量尾巴 + 出拼图
    python tools\siri_orb\marquee_check.py live <glob>    # 实机连拍：追亮头角度

角度定义跟渲染器一致：u = (theta - 0.125) mod 1，0=右下角，顺时针 0.125=下边、
0.375=左边、0.5=左上角、0.625=上边、0.875=右边。
输出刻意全 ASCII（本机控制台 GBK）。
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import glob
import json
import os
import re
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

W, H = 1440, 900
NBIN = 120


def u_grid(w, h):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx = (xx - (w - 1) / 2.0) / max(1.0, (w - 1) / 2.0)
    ny = (yy - (h - 1) / 2.0) / max(1.0, (h - 1) / 2.0)
    theta = np.mod(np.arctan2(ny, nx) / (2.0 * np.pi), 1.0)
    u = np.mod(theta - 0.125, 1.0)
    edge = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy))
    return u, edge


def profile_of(u, edge, vals, lo=2.0, hi=40.0, nbin=NBIN):
    m = (edge >= lo) & (edge < hi)
    ub = np.clip((u[m] * nbin).astype(int), 0, nbin - 1)
    vb = np.asarray(vals)[m]
    prof = np.zeros(nbin, dtype=np.float32)
    cnt = np.zeros(nbin, dtype=np.float32)
    np.add.at(prof, ub, vb)
    np.add.at(cnt, ub, 1.0)
    cnt[cnt < 1] = 1.0
    return prof / cnt


def crest_of(prof):
    k = int(np.argmax(prof))
    return k / float(len(prof)), float(prof[k])


def unwrap(seq):
    """把角度序列展开成连续位移（圈数）。"""
    out = [0.0]
    prev = seq[0]
    for a in seq[1:]:
        d = a - prev
        while d > 0.5:
            d -= 1.0
        while d < -0.5:
            d += 1.0
        out.append(out[-1] + d)
        prev = a
    return out


def fit_period(times, angles):
    """最小二乘拟合角速度 -> 一圈几秒。少数坏帧不影响斜率，比逐帧位移抗噪。"""
    un = unwrap(angles)
    slope = float(np.polyfit(np.asarray(times, dtype=np.float64),
                             np.asarray(un, dtype=np.float64), 1)[0])
    if slope <= 1e-6:
        return slope, float("inf")
    return slope, 1.0 / slope


def offline():
    from siri_orb import GlowRenderer
    import cfgstore
    cfg = cfgstore.load()      # config.json 不在时自动用 DEFAULTS 兜底
    period = float(cfg.get("marquee_period_sec", 6.0))
    trail = float(cfg.get("marquee_trail", 0.09))
    gr = GlowRenderer(W, H, origin=(0.9236 * W, 0.9711 * H),
                      marquee_period=period, marquee_trail=trail)
    u, edge = u_grid(gr.lw, gr.lh)
    rim = (edge * 3.0 >= 2.0) & (edge * 3.0 < 40.0)

    print("marquee_period_sec=%.2f  marquee_trail=%.3f" % (period, trail))
    # 采样步长要够大：慢速时每步只走 2 个 bin，跟峰的宽度接近会打平（像"没在动"）
    step_t = max(0.25, period / 24.0)
    ts = [i * step_t for i in range(int(period / step_t) + 1)]
    angles, peaks, profs = [], [], []
    for t in ts:
        on = gr.render(t, 1.0, "listening", None, 1.0)[..., 3]
        off = gr.render(t, 1.0, "listening", None, 0.0)[..., 3]
        d = np.clip(on - off, 0.0, None)
        prof = profile_of(u, edge, d)
        profs.append(prof)
        a, p = crest_of(prof)
        angles.append(a)
        peaks.append(p)

    # 角速度用最小二乘拟合（逐帧位移会被个别坏帧带偏）
    slope, measured = fit_period(ts, angles)
    print("frames=%d  t=%.1f..%.1fs" % (len(ts), ts[0], ts[-1]))
    print("crest angles: %s" % " ".join("%.3f" % a for a in angles))
    print("measured = %.2f s/lap (designed %.2f) -> dev %+.1f%%"
          % (measured, period, (measured / period - 1.0) * 100.0))
    print("peak delta alpha: min=%.3f max=%.3f mean=%.3f"
          % (min(peaks), max(peaks), sum(peaks) / len(peaks)))

    # 尾巴长度：亮头背后多远掉到峰值的 20%
    prof = profs[len(profs) // 2]
    k = int(np.argmax(prof))
    pk = prof[k]
    tail_len = 0.0
    for j in range(1, len(prof)):
        idx = (k - j) % len(prof)      # 背离方向 = 顺时针落后的一侧
        if prof[idx] <= 0.20 * pk:
            tail_len = j / float(len(prof))
            break
    print("tail: %.1f%% of perimeter (%.0f deg) at 20%% of peak"
          % (tail_len * 100.0, tail_len * 360.0))

    # 拼图：同一时刻不同位置的亮头
    from PIL import Image as PILImage
    bg = np.zeros((gr.lh, gr.lw, 3), dtype=np.float32)
    bg[..., 0] = 0.10
    bg[..., 1] = 0.11
    bg[..., 2] = 0.16
    tiles = []
    for i in range(8):
        t = i * period / 8.0
        img = gr.render(t, 1.0, "listening", None, 1.0)
        rgb = np.clip(img[..., :3], 0, 1)
        al = np.clip(img[..., 3:4], 0, 1)
        comp = bg * (1.0 - al) + rgb * al
        tiles.append(PILImage.fromarray((comp * 255).astype(np.uint8), "RGB"))
    tw, th = tiles[0].size
    sheet = PILImage.new("RGB", (tw * 4, th * 2), (24, 26, 34))
    for i, tl in enumerate(tiles):
        sheet.paste(tl, ((i % 4) * tw, (i // 4) * th))
    out = os.path.join(HERE, "marquee_sheet.png")
    sheet.save(out)
    print("wrote %s" % out)
    return 0


def xcorr_shift(a, b):
    """循环互相关：求 b 相对 a 前移了多少个 bin（1 bin = 1/NBIN 圈）。"""
    n = len(a)
    fa = np.fft.rfft(a - a.mean(), n=2 * n)
    fb = np.fft.rfft(b - b.mean(), n=2 * n)
    cc = np.fft.irfft(np.conj(fa) * fb, n=2 * n)
    k = int(np.argmax(cc[:n]))
    if cc[k] < 0:
        k = -k
    return k / float(n)


def live(pat):
    # 注意：排除 *_btn_*.png（那是按钮特写，尺寸跟整屏不一样）
    files = sorted([p for p in glob.glob(pat)
                    if re.search(r"_\d+\.png$", p) and "_btn_" not in os.path.basename(p)],
                   key=lambda p: int(re.search(r"_(\d+)\.png$", p).group(1)))
    if len(files) < 3:
        print("need >=3 frames")
        return 1
    # 帧的实际像素尺寸未必是 1440x900（ImageGrab 会受 DPI 虚拟化影响），
    # 按真实尺寸建角度网格，否则掩码对不上。
    fw, fh = Image.open(files[0]).size
    print("frame size = %dx%d" % (fw, fh))
    u, edge = u_grid(fw, fh)
    profs = []
    for p in files:
        a = np.asarray(Image.open(p).convert("RGB")).astype(np.float32) / 255.0
        lum = a.sum(axis=2) / 3.0
        profs.append(profile_of(u, edge, lum))
    # 基线取逐 bin 的时间最小值：首帧可能在扩散期，不能拿首帧当基线
    base = np.min(np.stack(profs), axis=0)
    d = np.stack([np.clip(p - base, 0.0, None) for p in profs])

    # 自检：符号对不对（合成一个已知前移 20 bin 的序列）
    syn = np.zeros(NBIN)
    syn[10] = 1.0
    st = xcorr_shift(syn, np.roll(syn, 20))
    print("self-test: known forward shift 20 bins -> recovered %.1f bins" % (st * NBIN))

    # 相邻帧位移（用峰值高度筛掉扩散期/暗帧）
    steps, heights = [], []
    for i in range(1, len(d)):
        pk = float(d[i].max())
        heights.append(pk)
        if pk < 0.05:
            steps.append(None)
            continue
        steps.append(xcorr_shift(d[i - 1], d[i]))
    ang = 0.0
    seq = [0.0]
    for s in steps:
        if s is None:
            seq.append(None)
            continue
        ang += s
        seq.append(ang)
    print("frames=%d" % len(files))
    print("peak lift : %s" % " ".join("%.4f" % h for h in heights))
    print("step(lap) : %s" % " ".join(("  n/a " if s is None else "%+.4f" % s) for s in steps))
    print("unwrapped : %s" % " ".join(("  n/a " if s is None else "%+.4f" % s) for s in seq))
    ok = [s for s in steps if s is not None]
    if ok:
        tot = sum(ok)
        print("total advance = %.3f lap over %d valid steps" % (tot, len(ok)))
        mono = all(ok[i + 1] >= ok[i] - 1e-6 for i in range(len(ok) - 1))
        print("monotonic = %s" % mono)

    # ---- 角速度：用亮头 argmax 逐帧展开（前几帧是淡入过渡，跳过）----
    import cfgstore
    cfg = cfgstore.load()      # config.json 不在时自动用 DEFAULTS 兜底
    period = float(cfg.get("marquee_period_sec", 6.0))
    crests = []
    for prof in d:
        k = int(np.argmax(prof))
        crests.append(k / float(NBIN))
    un = unwrap(crests)
    skip = 6   # 跳过「扩散 -> 跑马灯淡入」的过渡帧
    if len(un) > skip + 2:
        # 用文件 mtime 求真实帧间隔（连拍脚本不写时间戳，但 mtime 就是抓帧时刻）
        ts = [os.path.getmtime(p) for p in files]
        t0 = ts[0]
        times = [t - t0 for t in ts]
        slope, measured = fit_period(times[skip:], crests[skip:])
        print("crest angles : %s" % " ".join("%.3f" % c for c in crests))
        print("unwrapped    : %s" % " ".join("%.3f" % u2 for u2 in un))
        print("measured = %.2f s/lap (designed %.2f) -> dev %+.1f%%"
              % (measured, period, (measured / period - 1.0) * 100.0))
    return 0


def sheet(pat, picks=(6, 11, 15, 19), out_name="marquee_live_sheet.png"):
    """把指定帧拼成一张图：看亮头在不同时间落在哪条边上。"""
    files = sorted([p for p in glob.glob(pat)
                    if re.search(r"_\d+\.png$", p) and "_btn_" not in os.path.basename(p)],
                   key=lambda p: int(re.search(r"_(\d+)\.png$", p).group(1)))
    use = [files[i] for i in picks if i < len(files)]
    if len(use) < 2:
        print("not enough frames")
        return 1
    from PIL import Image as PILImage
    ims = [PILImage.open(p).convert("RGB") for p in use]
    tw, th = ims[0].size
    sc = 0.5
    tw, th = int(tw * sc), int(th * sc)
    out = PILImage.new("RGB", (tw * 2, th * 2), (20, 22, 30))
    for i, im in enumerate(ims):
        out.paste(im.resize((tw, th), PILImage.LANCZOS), ((i % 2) * tw, (i // 2) * th))
    dst = os.path.join(HERE, out_name)
    out.save(dst)
    print("wrote %s (frames %s)" % (dst, [os.path.basename(p) for p in use]))
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "offline"
    if mode == "live":
        pat = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "mq_*.png")
        sys.exit(live(pat))
    if mode == "sheet":
        pat = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "mq_*.png")
        picks = None
        if len(sys.argv) > 3:
            picks = tuple(int(x) for x in sys.argv[3].split(","))
        out_name = sys.argv[4] if len(sys.argv) > 4 else "marquee_live_sheet.png"
        if picks:
            sys.exit(sheet(pat, picks, out_name))
        sys.exit(sheet(pat, out_name=out_name))
    sys.exit(offline())
