# -*- coding: utf-8 -*-
"""中英双语实机验收。

做三件事，每件都留下截图作为证据：
  1) 设置面板在**英文**下的逐页渲染（真的 SettingsDialog 控件，不是画的图）
  2) 设置面板在屏幕上的**真窗口**截图（证明 live 面板确实是英文）
  3) 通过跨进程触发通道问一句，截屏看**卡片**上的英文状态字 + 中英对照回复

⚠️ 跑之前会记住 MiniMax Code 的标题条，跑完比对 —— 面板/卡片绝不能把会话切走。

用法： python tools\siri_orb\i18n_live.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

from PIL import Image, ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import cfgstore
import i18n

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

SW, SH = 2880, 1800
CARD_BOX = (520, 80, 2360, 500)     # 卡片区域（物理像素，够高能装下双语两段）
TITLE_STRIP = (505, 88, 795, 148)   # MiniMax Code 主面板标题字形
GUARD_DIFF_MAX = 0.05

_KEEP = []      # 留住 QWidget 的 Python 引用（否则 C++ 对象会被提前析构）


def find_windows(title_sub):
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if title_sub in buf.value:
            out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def win_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def title_strip():
    return ImageGrab.grab().crop(TITLE_STRIP)


def strip_diff(a, b):
    import numpy as np
    pa = np.asarray(a.convert("L"), dtype=np.int16)
    pb = np.asarray(b.convert("L"), dtype=np.int16)
    if pa.shape != pb.shape:
        return 1.0
    return float((np.abs(pa - pb) > 40).mean())


def ink(img, box):
    """区域里有多少「亮文字」像素 —— 用来挑内容最满的那一帧。"""
    import numpy as np
    a = np.asarray(img.convert("L").crop(box), dtype=np.int16)
    return int((a > 150).sum())


def save(img, name):
    """PIL Image 和 QPixmap 都能存（Pixmap 是渲染面板用的）。"""
    p = os.path.join(HERE, name)
    img.save(p)
    w = getattr(img, "width", None)
    h = getattr(img, "height", None)
    if callable(w):
        size = (w(), h())
    elif isinstance(w, int):
        size = (w, h)
    else:
        size = img.size
    print("  shot -> %s  %s" % (name, size), flush=True)
    return p


def set_lang(ui, reply):
    c = cfgstore.load()
    c["ui_lang"] = ui
    c["reply_lang"] = reply
    cfgstore.save(c)
    print("config -> ui_lang=%s reply_lang=%s" % (ui, reply), flush=True)


def render_tabs(tag):
    """把设置面板每一页都渲染成 PNG（真的控件 + 真的 QSS）。"""
    from PyQt5 import QtCore, QtWidgets
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    import settings as S
    # 自检不发真实请求（避免脚本退出时线程还在跑）
    S.SettingsDialog._backend_check = lambda self: None
    dlg = S.SettingsDialog()
    dlg.resize(900, 720)
    dlg.move(2900, 0)          # 挪到屏幕外，别挡住用户界面
    dlg.show()
    app.processEvents()
    time.sleep(0.4)
    app.processEvents()
    for i, tab in enumerate(cfgstore.TABS):
        dlg.tabs.setCurrentIndex(i)
        app.processEvents()
        time.sleep(0.25)
        app.processEvents()
        save(dlg.grab(), "i18n_%s_%s.png" % (tag, tab))
    dlg.hide()
    _KEEP.append(dlg)      # 别让 Python 引用一掉就把 C++ 对象析构掉
    return dlg


def live_settings_shot():
    """把设置面板真拉起来，截屏（证明 live 面板就是英文）。"""
    exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    p = subprocess.Popen([exe, os.path.join(HERE, "settings.py")], cwd=HERE,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    win = None
    for _ in range(40):
        time.sleep(0.5)
        hits = find_windows("Siri-OS")
        if hits:
            win = hits[0]
            break
    if not win:
        p.kill()
        print("!! 设置面板窗口没找到")
        return None
    time.sleep(1.2)
    r = win_rect(win[0])
    img = ImageGrab.grab().crop(r)
    save(img, "i18n_settings_en_live.png")
    try:
        p.kill()          # Qt 窗口不响应 terminate，必须 kill
        p.wait(timeout=5)
    except Exception:
        pass
    time.sleep(1.0)
    return win[1]


def ask_via_trigger(question, tag, frames=26, gap=0.7):
    """通过跨进程触发通道问一句，截卡片。

    ⚠️ 常驻助手「忙的时候丢弃新问题」（上一次回复还在播报）——
       所以触发前先等上一轮说完，否则这一轮的问题会被丢��。
    """
    time.sleep(6.0)          # 等上一轮播报结束
    cfgstore.fire_trigger(question)
    print("  trigger sent:", question, flush=True)
    best = None
    best_ink = -1
    for i in range(frames):
        time.sleep(gap)
        img = ImageGrab.grab().crop(CARD_BOX)
        n = ink(img, (0, 0, img.width, img.height))
        save(img, "i18n_%s_f%02d.png" % (tag, i))
        print("    frame %02d ink=%d" % (i, n), flush=True)
        if n > best_ink:
            best_ink = n
            best = img
    print("  best ink=%d" % best_ink, flush=True)
    return best


def main():
    guard_before = title_strip()
    saved = cfgstore.load()
    print("== 1) 英文面板逐页渲染 ==")
    set_lang("en", "both")
    render_tabs("en")

    print("== 2) live 设置面板真窗口 ==")
    live_settings_shot()

    print("== 3) 卡片：中英对照回复 ==")
    ask_via_trigger("今天几号", "card_local")

    print("== 4) 卡片：模型双语回答 ==")
    ask_via_trigger("用一句话介绍你自己", "card_llm", frames=30, gap=0.9)

    print("== 5) 还原成中文 + 中文回归截图 ==")
    set_lang("zh", "zh")
    time.sleep(1.5)
    ask_via_trigger("今天几号", "card_zh", frames=14, gap=0.8)
    time.sleep(1.2)
    guard_after = title_strip()
    d = strip_diff(guard_before, guard_after)
    print("  guard diff = %.4f (阈值 %.2f)" % (d, GUARD_DIFF_MAX))
    if d > GUARD_DIFF_MAX:
        title_strip().save(os.path.join(HERE, "i18n_guard_after.png"))
        print("!! GUARD: 标题条变了 —— 会话可能被误切！")
    else:
        print("guard ok: 会话标题未变")
    return 0


if __name__ == "__main__":
    sys.exit(main())
