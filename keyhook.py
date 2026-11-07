# -*- coding: utf-8 -*-
"""全局键盘钩子：注册自定义热键 + 嗅探/绑定指定扫描码的按键（小米语音助手键）。"""
import os
import time
import threading
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_HOTKEY = 0x0312

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0xBEEF

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_long
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

MOD_MAP = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN, "super": MOD_WIN,
}

VK_MAP = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "quote": 0xDE, "semicolon": 0xBA, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
    "lbracket": 0xDB, "rbracket": 0xDD, "minus": 0xBD, "equal": 0xBB, "backslash": 0xDC,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
}

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space",
    0x21: "PgUp", 0x22: "PgDn", 0x23: "End", 0x24: "Home", 0x25: "Left",
    0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2D: "Ins", 0x2E: "Del",
    0xAF: "VolUp", 0xAE: "VolDown", 0xAD: "VolMute", 0xB0: "MediaPlay",
    0xB1: "MediaNext", 0xB2: "MediaPrev", 0xB3: "MediaStop", 0xB4: "MediaPlayPause",
}


def parse_hotkey(spec):
    """'ctrl+alt+space' -> (mods, vk)"""
    if not spec:
        return 0, 0
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    mods, vk = 0, 0
    for p in parts:
        if p in MOD_MAP:
            mods |= MOD_MAP[p]
        else:
            vk = VK_MAP.get(p, 0)
    return mods, vk


def key_name(vk, sc, flags):
    ext = " ext" if flags & 0x01 else ""
    name = VK_NAMES.get(vk)
    if not name:
        if 0x30 <= vk <= 0x5A:
            name = chr(vk)
        elif 0x60 <= vk <= 0x69:
            name = "Num" + chr(vk - 0x60)
        else:
            name = "VK_0x%02X" % vk
    return "%s%s" % (name, ext)


class KeyHook(threading.Thread):
    def __init__(self, on_hotkey=None, on_match=None, hotkey="ctrl+alt+space",
                 scan_code=None, vk=None, log_path=None, log_all=True, swallow=True):
        threading.Thread.__init__(self)
        self.daemon = True
        self.on_hotkey = on_hotkey
        self.on_match = on_match
        self.hotkey = hotkey
        self.scan_code = scan_code
        self.vk = vk
        self.log_path = log_path
        self.log_all = log_all
        self.swallow = swallow
        self.error = None
        self.hotkey_error = None
        self._hook = None
        self._proc = None
        self._last_fire = 0.0

    def _log(self, vk, sc, flags):
        if not self.log_path:
            return
        try:
            d = os.path.dirname(self.log_path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            line = "%s vk=0x%02X(%d) sc=0x%02X(%d) flags=0x%02X %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), vk, vk, sc, sc, flags,
                key_name(vk, sc, flags))
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _match(self, vk, sc):
        if self.scan_code is not None and sc == int(self.scan_code):
            return True
        if self.vk is not None and vk == int(self.vk):
            return True
        return False

    def _fire(self, why):
        now = time.time()
        if now - self._last_fire < 0.45:
            return
        self._last_fire = now
        try:
            if why == "hotkey" and self.on_hotkey:
                self.on_hotkey()
            elif why == "match" and self.on_match:
                self.on_match(self.vk, self.scan_code)
        except Exception:
            pass

    def _callback(self, ncode, wparam, lparam):
        if ncode >= 0:
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    kbd = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk, sc, fl = int(kbd.vkCode), int(kbd.scanCode), int(kbd.flags)
                except Exception:
                    vk = sc = fl = 0
                if self.log_all:
                    self._log(vk, sc, fl)
                if self._match(vk, sc):
                    self._fire("match")
                    if self.swallow:
                        return 1
        return user32.CallNextHookEx(self._hook, ncode, wparam, lparam)

    def run(self):
        self._proc = HOOKPROC(self._callback)
        # 进程内低层钩子：hMod 必须为 NULL，否则报 ERROR_MOD_NOT_FOUND(126)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, ctypes.cast(self._proc, ctypes.c_void_p), None, 0)
        if not self._hook:
            self.error = "SetWindowsHookEx failed, err=%s" % kernel32.GetLastError()
            return
        if self.hotkey:
            mods, vk = parse_hotkey(self.hotkey)
            if not vk:
                self.hotkey_error = "bad hotkey spec: %s" % self.hotkey
            elif not user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
                self.hotkey_error = "RegisterHotKey failed for %s" % self.hotkey
        msg = wintypes.MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                self._fire("hotkey")
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self):
        try:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
        except Exception:
            pass
