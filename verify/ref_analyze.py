# -*- coding: utf-8 -*-
"""逆向分析参考视频（iOS 18 Siri 边光）的扩散动画。

目标：量化「从按钮点扩散到四周」的时序与空间规律，
用来在 Windows 上复刻同样的动画（而不是靠肉眼看帧）。

视频是 0.2x 慢放，所以视频时间 = 真实时间 * 5。
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys

import cv2
import numpy as np

VIDEO = sys.argv[1] if len(sys.argv) > 1 else None


def load(path):
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    return cap, fps, n, w, h


def ring_profile(frame, band=6):
    """取最外圈若干像素，返回每列/每行的平均亮度与色相（简化：只取亮度）。"""
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    top = gray[0:band, :].mean(axis=0)
    bottom = gray[h - band:h, :].mean(axis=0)
    left = gray[:, 0:band].mean(axis=1)
    right = gray[:, w - band:w].mean(axis=1)
    return top, right, bottom, left


def main():
    cap, fps, n, w, h = load(VIDEO)
    print("video: %dx%d  fps=%.3f  frames=%d  dur=%.2fs" % (w, h, fps, n, n / fps))
    # 抽帧：每 0.5 秒（视频时间）
    step = max(1, int(fps * 0.5))
    prev = None
    rows = []
    for i in range(0, n, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, fr = cap.read()
        if not ok:
            continue
        t = i / fps
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        # 四边亮度均值
        b = 3
        top = gray[:b, :].mean()
        bottom = gray[-b:, :].mean()
        left = gray[:, :b].mean()
        right = gray[:, -b:].mean()
        # 边缘最强点位置（找最亮的边界点，判断波头位置）
        rim = np.concatenate([
            gray[:b, :].mean(axis=0),
            gray[:, -b:].mean(axis=1),
            gray[-b:, :].mean(axis=0)[::-1],
            gray[:, :b].mean(axis=1)[::-1],
        ])
        hot = int(np.argmax(rim))
        rows.append((t, top, right, bottom, left, rim.max(), rim.min(), hot, len(rim)))
    print("%6s %7s %7s %7s %7s %7s %7s %8s" % ("t", "top", "right", "bottom", "left", "rimMax", "rimMin", "hotIdx"))
    for t, top, right, bottom, left, rmax, rmin, hot, ln in rows:
        print("%6.1f %7.2f %7.2f %7.2f %7.2f %7.2f %7.2f %8d" % (t, top, right, bottom, left, rmax, rmin, hot))
    cap.release()


if __name__ == "__main__":
    main()
