# -*- coding: utf-8 -*-
"""验托盘菜单的文案跟着「界面语言」实时变。

Siri-OS 的托盘图标藏在**溢出区**（任务栏那个 ^ 按钮后面），所以流程是：
点 ^ -> 飞出来一块图标面板 -> 找到品红色的 Siri-OS 图标 -> 右键 -> 截菜单。

全程只点任务栏，不碰 MiniMax Code 的会话栏；跑完还原鼠标位置。

用法： python tools\siri_orb\tray_lang_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import ctypes
import os
import sys
import time
from ctypes import wintypes

import numpy as np
from PIL import ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import cfgstore

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

CHEVRON = (2175, 1750)          # 任务栏溢出按钮（物理像素）
FLYOUT_REGION = (1700, 1150, 2500, 1700)
# 溢出面板打开后，Siri-OS 图标的实测位置（找颜色失败时的兜底）
ICON_FALLBACK = (2015, 1628)


def find_magenta(region):
    """在指定区域里找品红色的 Siri-OS 托盘图标，返回物理坐标。"""
    im = ImageGrab.grab().crop(region)
    a = np.asarray(im.convert("RGB")).astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    m = (r > 195) & (b > 165) & ((r - g) > 50) & ((b - g) > 20)
    ys, xs = np.nonzero(m)
    if len(xs) == 0:
        return None
    best = None
    start = 0
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    for i in range(1, len(xs) + 1):
        if i == len(xs) or xs[i] - xs[i - 1] > 8:
            seg = slice(start, i)
            n = i - start
            if best is None or n > best[0]:
                best = (n, int(xs[seg].mean()), int(ys[seg].mean()))
            start = i
    n, cx, cy = best
    return cx + region[0], cy + region[1], n


def click(x, y, right=False):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.35)
    down, up = (0x0008, 0x0010) if right else (0x0002, 0x0004)
    user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(up, 0, 0, 0, 0)


def esc():
    user32.keybd_event(0x1B, 0, 0, 0)
    user32.keybd_event(0x1B, 0, 2, 0)
    time.sleep(0.4)


def main():
    pos = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pos))
    print("cursor was at", pos.x, pos.y)
    try:
        print("1) open overflow flyout")
        click(*CHEVRON)
        time.sleep(1.4)
        hit = find_magenta(FLYOUT_REGION)
        if hit:
            x, y, n = hit
            print("   icon at", x, y, "pixels=", n)
        else:
            x, y = ICON_FALLBACK
            print("   icon detection failed -> fallback", x, y)

        print("2) right click the icon")
        click(x, y, right=True)
        time.sleep(1.3)
        out = sys.argv[1] if len(sys.argv) > 1 else "tray_menu_en.png"
        ImageGrab.grab().crop((max(0, x - 400), max(0, y - 460),
                               min(2880, x + 400), y + 80)).save(
            os.path.join(HERE, out))
        print("   shot -> %s" % out)
        esc()
        esc()
        time.sleep(0.6)
    finally:
        user32.SetCursorPos(pos.x, pos.y)
        print("cursor restored to", pos.x, pos.y)
    return 0


if __name__ == "__main__":
    sys.exit(main())
