# -*- coding: utf-8 -*-
"""Win+H（Windows 语音输入）接入层。

为什么放弃本机离线听写：这台机器的 Intel SST DMIC 采集链路在驱动层就断了 ——
winmm 能 open / prepare / start 都成功，但一个字节都收不到；SAPI 的 AudioLevel 恒 0。
自己开麦的听写方案在这台机器上是死的。

Win+H 走 Windows 自带的「语音输入」（Win11 起是 Microsoft 在线语音服务），
采集和识别都交给系统。它和本项目怎么配合：

  1. 触发键按下 -> 弹字幕卡 + 进入编辑态（QTextEdit 拿到键盘焦点）
  2. 程序发 Win+H -> Windows 的语音输入面板打开，开始识别
  3. Windows 把识别到的字**直接打进那个 QTextEdit**（因为它有焦点）
  4. 用户按 Enter -> 走和语音/打字完全一样的回复链路

⚠️ Win+H 往「有焦点的窗口」里打字，所以输入框必须真的有焦点。
⚠️ Win+H 仍然要麦克风 —— 采集链路不修好，它照样听不见。
   换一副已配对的蓝牙耳机是最快的绕法：绕开坏的 Intel SST 驱动。
"""
import ctypes
import time

VK_LWIN = 0x5B
VK_H = 0x48
KEYUP = 0x0002


def available():
    """语音输入（Win+H）要 Win10 1809 以上。"""
    try:
        return hasattr(ctypes.windll.user32, "keybd_event")
    except Exception:
        return False


def send_hotkey(delay=0.06):
    """程序化按下 Win+H，返回是否发出。"""
    try:
        u = ctypes.windll.user32
        u.keybd_event(VK_LWIN, 0, 0, 0)
        time.sleep(delay)
        u.keybd_event(VK_H, 0, 0, 0)
        time.sleep(0.03)
        u.keybd_event(VK_H, 0, KEYUP, 0)
        u.keybd_event(VK_LWIN, 0, KEYUP, 0)
        return True
    except Exception:
        return False


def force_foreground(win):
    """把窗口拉到前台，好让键盘焦点真的落到输入框上。

    Windows 不让后台进程随便抢焦点，AttachThreadInput 是标准绕法；
    触发键那一刻进程本来就在处理用户输入，抢焦点是合理的。
    """
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        # QWidget 不是 HWND —— 必须取 winId()，不然 ctypes 直接抛 TypeError
        hwnd = win if isinstance(win, int) else int(win.winId())
        fg = u.GetForegroundWindow()
        cur = k.GetCurrentThreadId()
        tgt = u.GetWindowThreadProcessId(fg, None) if fg else 0
        if tgt:
            u.AttachThreadInput(cur, tgt, True)
        u.ShowWindow(hwnd, 5)          # SW_SHOW
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        if tgt:
            u.AttachThreadInput(cur, tgt, False)
        return True
    except Exception:
        return False
