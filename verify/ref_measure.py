# -*- coding: utf-8 -*-
"""Reference-video quantitative measurement: steady palette + wavefront radial profile.

Two things we need from the reference and cannot guess:
  A) steady palette  -> which color pools exist, and how the transition is done
  B) radial profile  -> where the wavefront is, and how long the trail behind it is

Output is ASCII only (console here is GBK).
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import json
import numpy as np
import cv2

VIDEO = sys.argv[1] if len(sys.argv) > 1 else ""
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ref", "measure.json")


def frame_at(cap, t_sec, fps=30.0):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(t_sec * fps)))
    ok, f = cap.read()
    if not ok:
        return None
    return f


def ellipse_theta(H, W):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    nx = (xx - (W - 1) / 2.0) / max(1.0, (W - 1) / 2.0)
    ny = (yy - (H - 1) / 2.0) / max(1.0, (H - 1) / 2.0)
    return np.mod(np.arctan2(ny, nx) / (2.0 * np.pi), 1.0), xx, yy


def hsv_of(arr):
    h, s, v = cv2.split(cv2.cvtColor(arr.astype(np.uint8), cv2.COLOR_RGB2HSV))
    return h.astype(np.float32) / 180.0, s.astype(np.float32) / 255.0, v.astype(np.float32) / 255.0


def measure_palette(cap, times, nsec=48, band_px=90):
    """For each theta sector: median RGB of the rim band, and hue of the strongest pixels."""
    H = W = None
    theta = None
    out = {}
    for t_sec in times:
        f = frame_at(cap, t_sec)
        if f is None:
            continue
        rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        if theta is None or (h, w) != (H, W):
            H, W = h, w
            theta, xx, yy = ellipse_theta(H, W)
            edge = np.minimum(np.minimum(xx, W - 1 - xx), np.minimum(yy, H - 1 - yy))
            band = edge < band_px
        hs, ss, vs = hsv_of(rgb)
        rows = []
        for k in range(nsec):
            m = band & (theta >= k / nsec) & (theta < (k + 1) / nsec)
            n = int(m.sum())
            if n < 50:
                rows.append({"k": k, "theta": round((k + 0.5) / nsec, 4), "n": n})
                continue
            sel = rgb[m]
            med = np.median(sel, axis=0)
            # hue of the most saturated + bright 15% of pixels (the glow itself)
            score = ss[m] * 255.0 + vs[m] * 255.0
            thr = np.percentile(score, 85)
            strong = np.zeros_like(m)
            strong[m] = score >= thr
            hh = np.median(hs[strong]) if strong.sum() > 30 else float("nan")
            rows.append({"k": k, "theta": round((k + 0.5) / nsec, 4), "n": n,
                         "rgb": [int(v) for v in med],
                         "hue": (round(float(hh), 3) if hh == hh else None),
                         "sat": round(float(np.median(ss[strong])), 3) if strong.sum() > 30 else None,
                         "val": round(float(np.median(vs[strong])), 3) if strong.sum() > 30 else None})
        out["t%.1f" % t_sec] = rows
    return out


def measure_profile(cap, base_t, times, origin=None, rbin=100):
    """Radial gain profile around the wave origin -> front radius + trail length."""
    fb = frame_at(cap, base_t)
    if fb is None:
        return {}
    b = cv2.cvtColor(fb, cv2.COLOR_BGR2RGB).astype(np.float32)
    H, W = b.shape[:2]
    theta, xx, yy = ellipse_theta(H, W)
    if origin is None:
        origin = (W - 6, int(H * 0.25))
    ox, oy = float(origin[0]), float(origin[1])
    db = np.sqrt((xx - ox) ** 2 + (yy - oy) ** 2)
    nb = int(db.max() // rbin) + 1
    res = {}
    for t_sec in times:
        f = frame_at(cap, t_sec)
        if f is None:
            continue
        cur = cv2.cvtColor(f, cv2.COLOR_BGR2RGB).astype(np.float32)
        g = cur - b
        gm = g.mean(axis=2)
        prof = np.zeros(nb)
        for k in range(nb):
            m = (db >= k * rbin) & (db < (k + 1) * rbin)
            if m.sum():
                prof[k] = float(gm[m].mean())
        peak = int(np.argmax(prof))
        res["t%.2f" % t_sec] = {
            "rbin": rbin,
            "peak_r": peak * rbin,
            "profile": [round(float(x), 2) for x in prof],
        }
    return res


def main():
    if not VIDEO or not os.path.exists(VIDEO):
        print("VIDEO MISSING:", VIDEO)
        return 2
    cap = cv2.VideoCapture(VIDEO)
    if not cap.isOpened():
        print("cannot open video")
        return 2
    print("frames=%s fps=%s" % (cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS)))

    pal = measure_palette(cap, [11.0, 20.0, 45.0])
    print("=== PALETTE (rim band, 48 sectors) ===")
    for key, rows in pal.items():
        print("-- %s --" % key)
        for r in rows:
            print("  theta=%.3f n=%-6s rgb=%s hue=%s sat=%s val=%s" % (
                r["theta"], r["n"], r.get("rgb"), r.get("hue"), r.get("sat"), r.get("val")))

    prof = measure_profile(cap, 7.5, [8.8, 9.2, 9.6, 10.0, 10.4, 11.0, 12.0, 14.0, 20.0])
    print("=== RADIAL PROFILE (gain vs radius from origin) ===")
    for key, v in prof.items():
        p = v["profile"]
        rb = v.get("rbin", 100)
        top = " ".join("%d:%+.1f" % (i * rb, x) for i, x in enumerate(p) if x > 0.5)
        print("%s peak_r=%d" % (key, v["peak_r"]))
        print("   %s" % top)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"palette": pal, "profile": prof}, f, indent=1)
    print("saved", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
