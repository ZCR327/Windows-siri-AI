# -*- coding: utf-8 -*-
"""验证「按下语音键不再弹控制台窗口」。

同时盯两件事，避免「没弹窗」是假象：
  1. 有没有新冒出来的控制台窗口（枚举可见顶层窗口）
  2. 听写进程 powershell.exe 到底起没起（没起 = 按键压根没生效，弹窗检测无意义）

用法：
    python tools\siri_orb\nowin_check.py press    # 按键路径（走常驻实例）
    python tools\siri_orb\nowin_check.py control  # 对照：旧写法（pythonw 拉 powershell 不带 flag）
    python tools\siri_orb\nowin_check.py press --all   # 额外打印所有新窗口（含浮层）
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MARKERS = ("consolewindowclass", "cascadia_hosting_window_class", "powershell",
           "cmd.exe", "命令提示符", "windows terminal")


def list_windows():
    out = []
    user32 = ctypes.windll.user32
    proc_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append((int(hwnd), cls.value, buf.value, int(pid.value)))
        return True

    user32.EnumWindows(proc_t(cb), 0)
    return out


def console_like(w):
    blob = ("%s %s" % (w[1], w[2])).lower()
    return any(m in blob for m in MARKERS)


def count_powershell():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq powershell.exe", "/NH", "/FO", "CSV"],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = r.stdout.decode("utf-8", "ignore").lower()
        return txt.count("powershell.exe")
    except Exception:
        return -1


def watch(seconds, action=None, show_all=False):
    base_win = list_windows()
    base = set(w[0] for w in base_win)
    base_console = [w for w in base_win if console_like(w)]
    print("baseline: %d visible windows, %d console-like" % (len(base), len(base_console)))
    if action:
        action()
    seen = {}
    all_new = {}
    max_ps = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        for w in list_windows():
            if w[0] in base:
                continue
            all_new[w[0]] = w
            if console_like(w):
                seen[w[0]] = w
        n = count_powershell()
        if n > max_ps:
            max_ps = n
        time.sleep(0.15)
    print("RESULT: %d NEW CONSOLE WINDOW(S); max powershell.exe alive = %d"
          % (len(seen), max_ps))
    for h, cls, title, pid in seen.values():
        print("   [console] hwnd=%d class=%s title=%r pid=%d" % (h, cls, title, pid))
    if show_all:
        print("   all new windows (incl. overlay):")
        for h, cls, title, pid in all_new.values():
            print("     hwnd=%d class=%s title=%r pid=%d" % (h, cls, title, pid))
    return len(seen)


def do_press():
    import inject_scan
    time.sleep(0.3)
    inject_scan.send_scan(0x72)
    print("injected scan code 0x72")


def do_control():
    """对照：从 pythonw（无控制台）拉 powershell，且**故意不带** CREATE_NO_WINDOW。"""
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        print("pythonw not found: %s" % pythonw)
        return
    code = ("import subprocess,time;"
            "subprocess.Popen(['powershell','-NoProfile','-Command','Start-Sleep 6']);"
            "time.sleep(6)")
    subprocess.Popen([pythonw, "-c", code],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("spawned OLD-STYLE child (no CREATE_NO_WINDOW) -> expect a console window")


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = argv[0] if argv else "press"
    secs = float(argv[1]) if len(argv) > 1 else 6.0
    show_all = "--all" in sys.argv
    if mode == "control":
        n = watch(secs, do_control, show_all)
    else:
        n = watch(secs, do_press, show_all)
    return 0 if n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
