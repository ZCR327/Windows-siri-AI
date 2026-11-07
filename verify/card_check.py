# -*- coding: utf-8 -*-
"""实测：语音对话框能点、能编辑、Enter 能发送。

流程：注入按键 -> 卡片出现 -> 截图 -> 真实鼠标点击卡片 -> 截图（应出现输入框）
      -> SendInput 输入中文 -> 回车 -> 截图（应显示回复）
      -> 点击卡片外的任务栏空白处（应穿透到底下的桌面程序）

⚠️ 两条护栏（2026-11-07 补）：
  1) 「点击卡片外侧」的老落点是物理 (200, 900)，那正好在 MiniMax Code 的左侧
     会话栏上 —— 实测把用户的会话界面误切到了别的会话。现改为任务栏空白区
     (660, 1765)，点它只会把焦点交给桌面，不触发任何导航。
  2) 运行前后各抓一次主面板标题条并比对；一旦发现会话被切走，立刻存图报警。

用法： python tools\siri_orb\card_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import ctypes
import os
import sys
import time

from ctypes import wintypes
from PIL import ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import inject_scan

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

SW = 2880
SH = 1800
CLICK_X, CLICK_Y = 1440, 200          # 卡片正文区（物理像素）
OUTSIDE_X, OUTSIDE_Y = 660, 1765      # 任务栏空白处（安全落点，不会切会话）
# 只框标题字形本身（物理 505-795）。
# 注意：Siri-OS 卡片窗口的左边缘落在物理 x≈872（Qt 逻辑 440 × 2），
# 框太宽会把卡片圆角算进来 -> 误报「会话被切」。
TITLE_STRIP = (505, 88, 795, 148)     # 主面板标题字形区域（物理像素）
GUARD_DIFF_MAX = 0.05               # 允许 5% 像素差异（抗锯齿/流式刷新）

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def send_unicode(s):
    for ch in s:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = ki
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.02)


def send_enter():
    VK_RETURN = 0x0D
    for flags in (0, KEYEVENTF_KEYUP):
        ki = KEYBDINPUT(VK_RETURN, 0, flags, 0, None)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = ki
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.25)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP


def shot(name):
    p = os.path.join(HERE, name)
    ImageGrab.grab().crop((0, 0, SW, 340)).save(p)
    print("  shot -> %s" % name, flush=True)
    return p


def title_strip():
    """抓主面板标题条，用来发现「会话被误切」这种副作用。"""
    return ImageGrab.grab().crop(TITLE_STRIP)


def strip_diff(a, b):
    """两帧标题条的差异比例（0 = 完全相同，1 = 完全不同）。"""
    import numpy as np
    pa = np.asarray(a.convert("L"), dtype=np.int16)
    pb = np.asarray(b.convert("L"), dtype=np.int16)
    if pa.shape != pb.shape:
        return 1.0
    return float((np.abs(pa - pb) > 40).mean())


def guard_selftest(before):
    """自检：人为破坏标题条，判据必须报「明显变化」，否则守卫等于没装。"""
    bad = before.copy()
    bad.paste((20, 20, 20), (0, 0, bad.width, bad.height))
    d = strip_diff(before, bad)
    # 破坏后只有「标题字形像素」会变（实测约 21%），远高于容差即可判敏感；
    # 要求 > 3 倍容差，避免只判出「变了」却分不清是换标题还是别的抖动。
    ok = d > GUARD_DIFF_MAX * 3
    print("  guard self-test: corrupted-strip diff=%.3f (需 > %.3f) -> %s"
          % (d, GUARD_DIFF_MAX * 3, "OK" if ok else "FAIL"))
    return ok


def hit_window(x, y):
    """返回该物理坐标命中的 (hwnd, pid, 进程名)。

    Siri-OS 覆盖层是 WS_EX_TRANSPARENT，命中测试会直接跳过它，
    所以这里返回的应该是底下的桌面程序，而不是 pythonw.exe。
    """
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    k32 = ctypes.windll.kernel32
    name = "?"
    h = k32.OpenProcess(0x1000, False, pid.value)
    if h:
        cb = ctypes.create_unicode_buffer(256)
        sz = ctypes.c_ulong(256)
        if k32.QueryFullProcessImageNameW(h, 0, cb, ctypes.byref(sz)):
            name = cb.value.rsplit("\\", 1)[-1]
        k32.CloseHandle(h)
    return hwnd, pid.value, name


def main():
    old = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(old))
    print("cursor was at", old.x, old.y)
    before = title_strip()
    guard_selftest(before)
    try:
        print("1) inject key")
        inject_scan.send_scan(0x72)
        time.sleep(1.6)
        shot("card_1_show.png")

        print("2) click the card")
        click(CLICK_X, CLICK_Y)
        time.sleep(0.9)
        shot("card_2_edit.png")

        print("3) type + Enter")
        send_unicode("今天几号")
        time.sleep(0.5)
        shot("card_3_typed.png")
        send_enter()
        time.sleep(1.6)
        shot("card_4_reply.png")

        print("4) click outside the card (taskbar blank area, should pass through)")
        hwnd, pid, name = hit_window(OUTSIDE_X, OUTSIDE_Y)
        print("  hit window under overlay: hwnd=%d pid=%d %s" % (hwnd, pid, name), flush=True)
        click(OUTSIDE_X, OUTSIDE_Y)
        time.sleep(0.8)
        shot("card_5_outside.png")
    finally:
        user32.SetCursorPos(old.x, old.y)
        after = title_strip()
        d = strip_diff(before, after)
        print("  guard diff = %.4f (阈值 %.2f)" % (d, GUARD_DIFF_MAX))
        if d > GUARD_DIFF_MAX:
            before.save(os.path.join(HERE, "guard_title_before.png"))
            after.save(os.path.join(HERE, "guard_title_after.png"))
            print("!! GUARD: 主面板标题变了 —— 会话可能被误切！")
            print("   见 guard_title_before.png / guard_title_after.png")
        else:
            print("guard ok: 会话标题未变")
        print("cursor restored to", old.x, old.y)
    return 0


if __name__ == "__main__":
    sys.exit(main())
