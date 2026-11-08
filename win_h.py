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
import os
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


# ---------------------------------------------------------------- 语音输入 UI 抑制

# Win+H 弹出来的语音输入面板 / 提示卡，是 Windows 自己的 UI，**没有官方开关**。
# 只能自己藏。
#
# ⚠️ 最大的坑（本机实测）：**语音面板和输入法宿主是同一个进程**
# （TextInputHost.exe，在 SystemApps\SxS\MicrosoftWindows.61869836.InpApp_... 下）。
# 按进程名一刀切去藏，会顺手把**中文输入法的候选窗**藏掉 —— 打字就废了。
#
# 本机实测到的窗口事实（2026-11-08）：
#   TextInputHost.exe 顶层窗只有两个：
#     class=Windows.UI.Core.CoreWindow rect=(0,0,1440,900) title='Windows 输入体验'  <- 一直可见，**不能藏**
#     class=IME                         title='Default IME'                            <- 输入法组字窗，**不能藏**
#
# 所以规则按证据分层，宁可漏藏也绝不能乱藏：
#   R1 标题明确是语音 UI（语音输入 / 语音服务 / voice typing）且窗口不大  -> 藏
#   R2 独立语音进程（别的 Windows 版本上语音 UI 是独立进程）              -> 藏
#   R3 语音宿主进程里**触发之后新冒出来**的窗口（面板多半就是这种）      -> 藏
# 保护名单，一个都不碰：
#   - 我们自己的进程
#   - 类名是 IME / MSCTFIME 的窗口（输入法组字窗、候选窗）
#   - 标题像输入法自己的（Windows 输入体验 / Microsoft 拼音 ...）
#   - 触发之前就已经可见的窗口（比如那个全屏的宿主窗）

# 语音宿主进程（含和输入法同进程的那个）
_VOICE_HOST_PROCS = ("textinputhost.exe", "windowsinputexperience.exe",
                     "speecheexperiencehost.exe", "windowsvoiceaccess.exe",
                     "microsoftvoiceaccess.exe")
# 面板整个由它托管的独立进程 —— 这些进程里的窗口可以直接藏
_DEDICATED_VOICE_PROCS = ("windowsinputexperience.exe", "speecheexperiencehost.exe",
                          "windowsvoiceaccess.exe", "microsoftvoiceaccess.exe")
# 标题里明确是语音 UI 的关键词
_VOICE_UI_TITLES = ("语音输入", "语音服务", "voice typing", "voice input")
# 输入法相关：类名精确匹配（别用 "ime" 子串，"Time" 里就有 "ime"）
_IME_CLASSES = ("ime", "msctfime ui", "msctfime")
# 输入法相关：标题关键词
_IME_TITLES = ("windows 输入体验", "microsoft 拼音", "微软拼音",
               "microsoft pinyin", "default ime")
# 语音面板 / 提示卡都不大；这个上限用来防止误伤同名的大窗口（比如浏览器标题）
_VOICE_UI_MAX_W = 900
_VOICE_UI_MAX_H = 520

_SW_HIDE = 0
_ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _win_rect(hwnd):
    r = _RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _class_is_ime(cls):
    low = (cls or "").strip().lower()
    return low in _IME_CLASSES or low.startswith("msctfime")


def _title_is_ime(title):
    low = (title or "").strip().lower()
    for k in _IME_TITLES:
        if k in low:
            return True
    return False


def _proc_name(pid):
    """进程名（小写）。拿不到就返回空串。"""
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(512)
        try:
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(h)
    except Exception:
        pass
    return ""


def _iter_windows():
    """所有顶层窗口：(hwnd, pid, class, title)。"""
    out = []
    u = ctypes.windll.user32

    def cb(h, l):
        try:
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cls, 256)
            n = u.GetWindowTextLengthW(h)
            ttl = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, ttl, n + 1)
            pid = ctypes.c_ulong()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            out.append((h, pid.value, cls.value, ttl.value))
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(_ENUMPROC(cb), 0)
    except Exception:
        pass
    return out


def is_voice_ui(hwnd, pid, cls, title, my_pid=None, was_visible=False):
    """这个窗口是不是「Windows 语音输入」的 UI？

    was_visible: 触发之前它是不是就已经可见（用来区分「新冒出来的面板」和
    「一直都在的输入法宿主窗」）。
    """
    try:
        if my_pid is not None and int(pid) == int(my_pid):
            return False
        if _class_is_ime(cls):
            return False
        if _title_is_ime(title):
            return False
        low = (title or "").lower()
        title_hit = False
        for key in _VOICE_UI_TITLES:
            if key.lower() in low:
                title_hit = True
                break
        if title_hit:
            # 语音面板和提示卡都不大；同名的大窗口（例如浏览器标题）不碰。
            # ⚠️ 这里**只能不成立、不能直接否掉** —— 还要往下走进程规则
            #    （面板在语音宿主进程里，窗口大小未必对得上）。
            try:
                l, t, w, h = _win_rect(hwnd)
                if w <= _VOICE_UI_MAX_W and h <= _VOICE_UI_MAX_H:
                    return True
            except Exception:
                return True
        name = _proc_name(pid)
        if name in _DEDICATED_VOICE_PROCS:
            return True
        if name in _VOICE_HOST_PROCS:
            # 和输入法同进程的那个：只藏**触发之后新冒出来**的窗口
            return not was_visible
        return False
    except Exception:
        return False


def suppress_voice_ui(duration=3.0, interval=0.12, on_hide=None):
    """Win+H 之后把语音输入 UI 藏起来（后台线程轮询，面板是异步弹出的）。

    on_hide: 每藏掉一个窗口回调一次（留痕用），参数是描述串。
    """
    import threading
    u = ctypes.windll.user32
    my_pid = os.getpid()

    before = set()
    try:
        for (h, pid, cls, title) in _iter_windows():
            if u.IsWindowVisible(h):
                before.add(h)
    except Exception:
        pass

    def run():
        end = time.time() + max(0.5, float(duration or 3.0))
        while time.time() < end:
            try:
                for (h, pid, cls, title) in _iter_windows():
                    if not u.IsWindowVisible(h):
                        continue
                    if is_voice_ui(h, pid, cls, title, my_pid, h in before):
                        try:
                            u.ShowWindow(h, _SW_HIDE)
                            if on_hide:
                                on_hide("hwnd=%s cls=%r title=%r pid=%s" % (h, cls, title, pid))
                        except Exception:
                            pass
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
# ---------------------------------------------------------------- 开始录音的声音
#
# 为什么 waveOutSetVolume 压不住：那个接口只管**本进程**的 waveOut 会话
# （实测：子进程把音量压成 0，父进程读回来还是 0xffffffff），
# 压不到别的进程播出来的声音。
#
# 那声「叮」到底是谁播的（2026-11-08 本机实测）：
#   音频会话[0] = 系统声音（显示名 @...AudioSrv.Dll,-202）-> 按 Win+H 全程 Inactive，**不是它**
#   音频会话[1] = Siri-OS.exe（我们自己）
#   音频会话[2] = TextInputHost.exe（Windows 语音输入宿主）-> 按 Win+H 后变 Active，**就是它**
# 结论：要压的是**语音输入宿主那个会话**的音量（音量合成器里那一档），
# 只压这一小会儿（默认 1.8 秒），到期原样恢复，别的声音一点不受影响。
#
# 附带说明：系统声音会话也一并压掉（别的 Windows 版本上提示音可能走那条路），
# 代价是这 1.8 秒里如果有通知提示音也会被吞掉 —— 比「按一次键就『叮』一声」划算。

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
_IID_IAudioSessionControl2 = "{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}"
_IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"

_CLSCTX_ALL = 23
_E_RENDER = 0
_ROLE_CONSOLE = 0
_ROLE_MULTIMEDIA = 1

# IAudioSessionControl2 的 vtable 槽位是**实测**出来的（不能照抄记忆）：
#   12 = 取会话实例标识（里面直接带宿主 exe 路径）
#   14 = 取宿主 PID
# 两者互相印证过：12 给出 TextInputHost.exe，14 给出同一个进程的 PID。
_VT_SESSION_INSTANCE_ID = 12
_VT_SESSION_PID = 14

# 语音输入宿主在不同 Windows 版本上的进程名
_VOICE_HOST_EXES = ("textinputhost.exe", "speechexperiencehost.exe",
                    "windowsinputexperience.exe", "windowsvoiceaccess.exe",
                    "microsoftvoiceaccess.exe")

_ole32 = ctypes.windll.ole32
_ole32.CoCreateInstance.restype = ctypes.c_long
_ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    g = _GUID()
    if _ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g)) != 0:
        raise OSError("bad guid")
    return g


def _vt(ptr, idx, restype, argtypes):
    """取 vtable 第 idx 个方法。

    ⚠️ COM 接口指针的第 0 格才是 vtable 指针；直接读 ptr[idx] 拿到的是
    接口对象自己的内存 -> 垃圾地址 -> 调用即崩（踩过）。
    """
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[idx])


def _release(ptr):
    if ptr:
        try:
            _vt(ptr, 2, ctypes.c_ulong, [])(ptr)
        except Exception:
            pass


def _query_interface(ptr, iid):
    out = ctypes.c_void_p()
    hr = _vt(ptr, 0, ctypes.c_long, [ctypes.c_void_p, ctypes.c_void_p])(
        ptr, ctypes.byref(_guid(iid)), ctypes.byref(out))
    return out.value if hr == 0 else None


def _com_init():
    """COM 必须**每个线程**各初始化一次。"""
    try:
        _ole32.CoInitializeEx(None, 2)   # COINIT_APARTMENTTHREADED
    except Exception:
        pass


class _AudioSession(object):
    """一个音频会话 + 它能调音量的那只手柄。"""

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.display_name = ""
        self.owner = ""          # 宿主进程名（小写）
        self.pid = 0
        self.volume = None
        self.muted = None
        self.volume_obj = _query_interface(ctrl, _IID_ISimpleAudioVolume)
        self._read()

    def _read(self):
        try:
            p = ctypes.c_wchar_p()
            if _vt(self.ctrl, 4, ctypes.c_long, [ctypes.POINTER(ctypes.c_wchar_p)])(
                    self.ctrl, ctypes.byref(p)) == 0 and p.value:
                self.display_name = p.value
                _ole32.CoTaskMemFree(p)
        except Exception:
            pass
        self._read_owner()
        if not self.volume_obj:
            return
        try:
            f = ctypes.c_float()
            if _vt(self.volume_obj, 4, ctypes.c_long,
                   [ctypes.POINTER(ctypes.c_float)])(self.volume_obj, ctypes.byref(f)) == 0:
                self.volume = round(f.value, 4)
            m = ctypes.c_int()
            if _vt(self.volume_obj, 6, ctypes.c_long,
                   [ctypes.POINTER(ctypes.c_int)])(self.volume_obj, ctypes.byref(m)) == 0:
                self.muted = m.value
        except Exception:
            pass

    def _read_owner(self):
        """宿主是谁：优先用 PID 反查 exe 名，拿不到再用会话实例标识里的路径。"""
        try:
            ctrl2 = _query_interface(self.ctrl, _IID_IAudioSessionControl2)
            if not ctrl2:
                return
            pid = ctypes.c_uint()
            try:
                if _vt(ctrl2, _VT_SESSION_PID, ctypes.c_long,
                       [ctypes.POINTER(ctypes.c_uint)])(ctrl2, ctypes.byref(pid)) == 0:
                    self.pid = int(pid.value)
                    name = _proc_name(self.pid)
                    if name:
                        self.owner = name
            except Exception:
                pass
            if not self.owner:
                try:
                    p = ctypes.c_wchar_p()
                    if _vt(ctrl2, _VT_SESSION_INSTANCE_ID, ctypes.c_long,
                           [ctypes.POINTER(ctypes.c_wchar_p)])(ctrl2, ctypes.byref(p)) == 0 and p.value:
                        ident = p.value
                        _ole32.CoTaskMemFree(p)
                        low = ident.lower()
                        for name in _VOICE_HOST_EXES:
                            if name in low:
                                self.owner = name
                                break
                except Exception:
                    pass
            _release(ctrl2)
        except Exception:
            pass

    def set_volume(self, level):
        if not self.volume_obj:
            return False
        try:
            return _vt(self.volume_obj, 3, ctypes.c_long,
                       [ctypes.c_float, ctypes.c_void_p])(
                self.volume_obj, ctypes.c_float(level), None) == 0
        except Exception:
            return False

    def set_mute(self, flag):
        if not self.volume_obj:
            return False
        try:
            return _vt(self.volume_obj, 5, ctypes.c_long,
                       [ctypes.c_int, ctypes.c_void_p])(
                self.volume_obj, 1 if flag else 0, None) == 0
        except Exception:
            return False

    def key(self):
        """同一会话在多次枚举里的稳定标识。"""
        return (self.pid or 0, self.display_name or "")

    def free(self):
        if self.volume_obj:
            _release(self.volume_obj)
            self.volume_obj = None
        if self.ctrl:
            _release(self.ctrl)
            self.ctrl = None


def list_audio_sessions(role=_ROLE_MULTIMEDIA):
    """列出默认播放设备上的音频会话。失败就返回空表（不让调用方炸）。"""
    out = []
    enum = ctypes.c_void_p()
    _com_init()          # COM 是**每线程**初始化的，忘了这步枚举会返回空表
    try:
        hr = _ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_MMDeviceEnumerator)), None,
                                     _CLSCTX_ALL, ctypes.byref(_guid(_IID_IMMDeviceEnumerator)),
                                     ctypes.byref(enum))
        if hr != 0 or not enum.value:
            return out
        dev = ctypes.c_void_p()
        hr = _vt(enum.value, 4, ctypes.c_long,
                 [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)])(
            enum.value, _E_RENDER, role, ctypes.byref(dev))
        # 注意：enum 只在 finally 里 Release 一次 —— 释放两次就是 over-release，
        # 之后枚举会莫名其妙地返回空表。
        if hr != 0 or not dev.value:
            return out
        mgr = ctypes.c_void_p()
        hr = _vt(dev.value, 3, ctypes.c_long,
                 [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                  ctypes.POINTER(ctypes.c_void_p)])(
            dev.value, ctypes.byref(_guid(_IID_IAudioSessionManager2)), _CLSCTX_ALL,
            None, ctypes.byref(mgr))
        _release(dev.value)
        if hr != 0 or not mgr.value:
            return out
        enumerator = ctypes.c_void_p()
        hr = _vt(mgr.value, 5, ctypes.c_long,
                 [ctypes.POINTER(ctypes.c_void_p)])(mgr.value, ctypes.byref(enumerator))
        _release(mgr.value)
        if hr != 0 or not enumerator.value:
            return out
        count = ctypes.c_int()
        _vt(enumerator.value, 3, ctypes.c_long,
            [ctypes.POINTER(ctypes.c_int)])(enumerator.value, ctypes.byref(count))
        for i in range(max(0, count.value)):
            ctrl = ctypes.c_void_p()
            hr = _vt(enumerator.value, 4, ctypes.c_long,
                     [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)])(
                enumerator.value, i, ctypes.byref(ctrl))
            if hr != 0 or not ctrl.value:
                continue
            try:
                out.append(_AudioSession(ctrl.value))
            except Exception:
                _release(ctrl.value)
        _release(enumerator.value)
    except Exception:
        pass
    finally:
        if enum.value:
            _release(enum.value)
    return out


def _is_system_sounds(session):
    return "audiosrv.dll" in (session.display_name or "").lower()


def _should_mute(session, my_pid=None):
    """这个会话要不要压？"""
    try:
        if my_pid and session.pid == int(my_pid):
            return False            # 我们自己的播报不能被压掉
        if session.owner in _VOICE_HOST_EXES:
            return True             # 语音输入宿主 —— 那声「叮」就是它
        if _is_system_sounds(session):
            return True             # 别的 Windows 版本上提示音可能走这条
    except Exception:
        pass
    return False


def mute_voice_start_sound(seconds=1.8, interval=0.08, on_change=None):
    """把「刚开始录音那声叮」压掉，只压这一小会儿。

    做法：找到语音输入宿主名下的音频会话 -> 音量压到 0 并 mute，
    到期按原值恢复。其它应用的声音一点不受影响。

    在 Win+H **发出去之前**调用最好：那时候就把会话压住，等提示音播就无声了。
    """
    import threading
    my_pid = os.getpid()

    def run():
        _com_init()
        saved = []          # [(session, volume, muted)]
        seen = set()
        end = time.time() + max(0.2, float(seconds or 1.8))
        try:
            while time.time() < end:
                try:
                    for role in (_ROLE_MULTIMEDIA, _ROLE_CONSOLE):
                        for s in list_audio_sessions(role):
                            try:
                                if not _should_mute(s, my_pid):
                                    s.free()
                                    continue
                                k = s.key()
                                if k in seen:
                                    s.free()
                                    continue
                                seen.add(k)
                                saved.append((s, s.volume, s.muted))
                                s.set_volume(0.0)
                                s.set_mute(True)
                                if on_change:
                                    on_change("muted owner=%s pid=%s vol=%s mute=%s" % (
                                        s.owner or "?", s.pid, s.volume, s.muted))
                            except Exception:
                                pass
                except Exception:
                    pass
                time.sleep(interval)
        finally:
            for s, vol, muted in saved:
                try:
                    if vol is not None:
                        s.set_volume(vol)
                    if muted is not None:
                        s.set_mute(bool(muted))
                    if on_change:
                        on_change("restored owner=%s pid=%s -> vol=%s mute=%s" % (
                            s.owner or "?", s.pid, vol, muted))
                except Exception:
                    pass
                s.free()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
