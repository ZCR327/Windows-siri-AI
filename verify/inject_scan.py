import _bootstrap  # noqa: F401  (repo root on sys.path)
import ctypes, sys
"""向系统注入一个原始扫描码，用来验证按键绑定是否生效（不用真按键盘）。

用法：python inject_scan.py 0x72
"""
user32 = ctypes.windll.user32
INPUT_KEYBOARD = 1
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def send_scan(sc):
    for flags in (KEYEVENTF_SCANCODE, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP):
        ki = KEYBDINPUT(0, sc, flags, 0, None)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = ki
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


if __name__ == "__main__":
    sc = int(sys.argv[1], 0)
    send_scan(sc)
    print("injected scan 0x%02X" % sc)
