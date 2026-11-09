import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PyQt5 import QtCore, QtGui
import siri_orb as SO

W, H, SCALE, THR, TS = 1440, 900, 6, 8, 8


def to_np(img):
    h = img.height(); w = img.width(); bpl = img.bytesPerLine()
    ptr = img.constBits(); ptr.setsize(img.byteCount())
    buf = ptr.asstring(img.byteCount())
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, bpl // 4, 4)[:, :w, :]


def paint_full(img):
    out = QtGui.QImage(W, H, QtGui.QImage.Format_RGBA8888)
    out.fill(0)
    p = QtGui.QPainter(out)
    p.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
    p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
    p.drawImage(QtCore.QRect(0, 0, W, H), img)
    p.end()
    return out


def paint_region(canvas, img, rects):
    rg = QtGui.QRegion()
    for rc in rects:
        rg = rg.united(QtGui.QRegion(rc))
    p = QtGui.QPainter(canvas)
    p.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
    p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
    p.setClipRegion(rg)
    p.drawImage(QtCore.QRect(0, 0, W, H), img)
    p.end()


def over_bg(img, bg=(24, 24, 30)):
    """Composite over an opaque background: what the user actually sees."""
    a = to_np(img).astype(np.float32)
    al = (a[..., 3:4] / 255.0)
    out = a[..., :3] * al + np.array(bg, dtype=np.float32) * (1.0 - al)
    return out


def main():
    gr = SO.GlowRenderer(W, H, scale=SCALE)
    canvas = QtGui.QImage(W, H, QtGui.QImage.Format_RGBA8888)
    canvas.fill(0)
    prev = None
    force_full = 0
    worst = 0.0
    worst_frame = -1
    plan = []
    for i in range(6):
        plan.append(dict(wave={'radius': 120.0 + 260.0 * i, 'width': 46.0},
                        level=0.0, intensity=1.0))
    for i in range(4):
        plan.append(dict(wave=None, level=0.0, intensity=1.0))
    for i in range(16):
        plan.append(dict(wave=None, level=0.95 if (i // 4) % 2 else 0.0,
                        intensity=1.0))
    for i in range(6):
        plan.append(dict(wave=None, level=0.0, intensity=1.0 - 0.15 * i))
    for idx, spec in enumerate(plan):
        arr = gr.render(idx * 0.03, spec['intensity'], 'thinking', spec['wave'],
                        1.0, spec['level'], u8=True)
        img = SO._to_qimage_u8(arr)
        spans = SO.glow_visible_spans(arr[..., 3], THR, TS)
        if spec['wave'] is not None:
            force_full = 1; prev = None; rects = None
        elif force_full > 0:
            force_full -= 1; prev = None; rects = None
        else:
            rects = SO.glow_span_rects(spans, SCALE, W, H)
            if prev:
                rects = rects + SO.glow_span_rects(prev, SCALE, W, H)
            prev = spans
        if rects is None:
            canvas = paint_full(img)
        else:
            paint_region(canvas, img, rects)
        truth = paint_full(img)
        d = np.abs(over_bg(canvas) - over_bg(truth)).max(axis=-1)
        m = float(d.max())
        if m > worst:
            worst = m
            worst_frame = idx
        if m > 10.0:
            print('  frame %02d: max visible diff %.1f/255' % (idx, m))
    print('frames: %d' % len(plan))
    print('WORST visible diff = %.1f/255 (frame %d)  [expected <= %d]'
          % (worst, worst_frame, THR))
    return 0 if worst <= THR else 1


if __name__ == '__main__':
    sys.exit(main())
