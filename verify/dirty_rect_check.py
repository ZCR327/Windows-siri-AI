import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
import siri_orb as SO

W, H, SCALE, THR, TS = 1440, 900, 6, 8, 8


def to_np(img):
    h = img.height()
    w = img.width()
    bpl = img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(img.byteCount())
    buf = ptr.asstring(img.byteCount())
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl // 4, 4)[:, :w, :]


def region_of(rects):
    rg = QtGui.QRegion()
    for rc in rects:
        rg = rg.united(QtGui.QRegion(rc))
    return rg


def mask_of(rects, w=W, h=H):
    m = np.zeros((h, w), dtype=bool)
    for rc in rects:
        x, y = int(rc.x()), int(rc.y())
        ww, hh = int(rc.width()), int(rc.height())
        if ww <= 0 or hh <= 0:
            continue
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(w, x + ww), min(h, y + hh)
        if x1 > x0 and y1 > y0:
            m[y0:y1, x0:x1] = True
    return m


def main():
    app = QtWidgets.QApplication(sys.argv)
    checks = 0
    fails = 0
    gr = SO.GlowRenderer(W, H, scale=SCALE)
    cases = [
        ("dim", dict(intensity=0.15, mode="idle", level=0.0)),
        ("normal", dict(intensity=1.0, mode="listening", level=0.0)),
        ("loud", dict(intensity=1.0, mode="listening", level=1.0)),
    ]
    for name, kw in cases:
        arr = gr.render(1.0, kw["intensity"], kw["mode"], None,
                        1.0, kw["level"], u8=True)
        al = arr[..., 3]
        spans = SO.glow_visible_spans(al, THR, TS)
        rects = SO.glow_span_rects(spans, SCALE, W, H)
        region = region_of(rects)
        ys, xs = np.nonzero(al >= THR)
        bad = 0
        for rr, cc in zip(ys.tolist(), xs.tolist()):
            if not region.contains(QtCore.QPoint(cc * SCALE, rr * SCALE)):
                bad += 1
        checks += 1
        if bad:
            fails += 1
            print("FAIL coverage %s: %d visible px outside the dirty area" % (name, bad))
        else:
            area = int(mask_of(rects).sum())
            print("ok coverage %s: spans=%d dirty=%d%% of screen (visible px=%d)"
                  % (name, len(spans), area * 100 // (W * H), len(ys)))
        img = SO._to_qimage_u8(arr)
        full = QtGui.QImage(W, H, QtGui.QImage.Format_RGBA8888)
        full.fill(0)
        p = QtGui.QPainter(full)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        p.drawImage(QtCore.QRect(0, 0, W, H), img)
        p.end()
        clip = QtGui.QImage(W, H, QtGui.QImage.Format_RGBA8888)
        clip.fill(0)
        p = QtGui.QPainter(clip)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        p.setClipRegion(region)
        p.drawImage(QtCore.QRect(0, 0, W, H), img)
        p.end()
        fa = to_np(full).astype(np.int16)
        ca = to_np(clip).astype(np.int16)
        mk = mask_of(rects)
        d = np.abs(fa - ca)
        # bilinear upscale differs by 1 LSB right on the clip edge -> acceptable
        inside_max = int(d[mk].max()) if mk.any() else 0
        n_diff = int((d[mk].max(axis=-1) > 0).sum()) if mk.any() else 0
        checks += 1
        if inside_max > 1:
            fails += 1
            print("FAIL pixels inside the dirty area differ: %d" % inside_max)
        else:
            print("ok pixels inside the dirty area match (max %d LSB, %d px differ)"
                  % (inside_max, n_diff))
        outside = ~mk
        amax = int(d[..., 3][outside].max()) if outside.any() else 0
        # a buffer alpha of 7/255 interpolates to 8/255 -> keep 1 of slack
        checks += 1
        if amax > THR:
            fails += 1
            print("FAIL alpha outside the dirty area too visible: %d" % amax)
        else:
            print("ok alpha outside the dirty area <= %d (invisible)" % amax)

    # ---- wave: the ring sits in the middle -> a full repaint is mandatory
    wave = {"radius": 500.0, "width": 46.0}
    arr = gr.render(1.0, 1.0, "listening", wave, 1.0, 0.0, u8=True)
    al = arr[..., 3]
    spans = SO.glow_visible_spans(al, THR, TS)
    rects = SO.glow_span_rects(spans, SCALE, W, H)
    vis = al >= THR
    bmask = np.zeros(al.shape, dtype=bool)
    for r0, r1, c0, c1 in spans:
        bmask[r0:r1, c0:c1] = True
    outside_vis = int((vis & ~bmask).sum())
    # the wave ring is thin, so it happens to fall inside the tiles --
    # but it moves ~2 buffer px per frame, so the previous tile still has
    # the ring in it -> a full repaint is the only safe thing here
    print("ok wave case: ring px outside the tiles = %d (ring moves -> full repaint)"
          % outside_vis)

    ov = SO.Overlay({})
    ov.setGeometry(0, 0, W, H)
    ov._spans = spans
    ov._wave_rect = ov._wave_bbox(wave)
    rects = ov._dirty_rects()
    checks += 1
    if rects is not None:
        fails += 1
        print("FAIL wave must force a full repaint")
    else:
        print("ok wave forces a full repaint")
    ov._wave_rect = None
    rects = ov._dirty_rects()
    checks += 1
    if rects is not None:
        fails += 1
        print("FAIL the frame right after the wave must still be full")
    else:
        print("ok the frame right after the wave is still full (erases the ring)")
    rects = ov._dirty_rects()
    checks += 1
    if rects is None or len(rects) < 3:
        fails += 1
        print("FAIL expected a strip repaint after the wave")
    else:
        print("ok strip repaint after the wave: %d rects" % len(rects))

    # ---- the shrinking glow must not leave a stale ring: the previous frame's
    #      spans have to be repainted as well
    ov2 = SO.Overlay({})
    ov2.setGeometry(0, 0, W, H)
    big = SO.glow_visible_spans(gr.render(1.0, 1.0, "listening", None, 1.0, 1.0, u8=True)[..., 3], THR, TS)
    small = SO.glow_visible_spans(gr.render(1.0, 1.0, "listening", None, 1.0, 0.0, u8=True)[..., 3], THR, TS)
    ov2._spans = small
    ov2._prev_spans = big
    rects = ov2._dirty_rects()
    n_big = len(SO.glow_span_rects(big, SCALE, W, H))
    checks += 1
    if rects is None or len(rects) < n_big:
        fails += 1
        print("FAIL the previous frame's area is not repainted")
    else:
        print("ok the previous frame's area is repainted too (%d rects)" % len(rects))

    print("%d checks, %d FAIL" % (checks, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
