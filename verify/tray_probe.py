# -*- coding: utf-8 -*-
"""量准任务栏右下角：电量 / 时钟 / 日期各自的位置。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, time
import numpy as np
from PIL import Image, ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H = 2880, 1800

def main():
    time.sleep(0.3)
    im = ImageGrab.grab()
    im.save(os.path.join(HERE, "tmp", "tray_full.png"))
    # 任务栏右下角（物理像素）
    box = (2450, 1690, 2880, 1800)
    crop = im.crop(box).resize(((box[2]-box[0])*3, (box[3]-box[1])*3), Image.LANCZOS)
    crop.save(os.path.join(HERE, "tmp", "tray_zoom.png"))
    a = np.asarray(im.convert("L")).astype(np.float32)
    band = a[1700:1795, 2450:2880]
    col = (band > 120).sum(axis=0)
    runs = []
    inrun = False
    for i, v in enumerate(col):
        if v > 0 and not inrun:
            inrun = True; start = i
        elif v == 0 and inrun:
            inrun = False
            if i - start >= 2:
                runs.append((2450 + start, 2450 + i - 1))
    if inrun:
        runs.append((2450 + start, 2450 + len(col) - 1))
    print("bright column runs (physical x):", runs)
    # 逐行看右侧区域，区分两行文字（时间 / 日期）
    right = a[1690:1800, 2600:2880]
    row = (right > 120).sum(axis=1)
    for i, v in enumerate(row):
        if v > 0:
            print("y=%d  n=%d" % (1690 + i, v))


if __name__ == "__main__":
    main()
