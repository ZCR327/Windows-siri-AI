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
import threading
import time
from ctypes import wintypes

# ⚠️ 静音窗口的全局状态（2026-11-09 22:05 修）
#   两次静音窗口重叠时，旧实现会把「已经静音」当成原始值存下来、到期再恢复回去
#   -> 语音输入宿主的会话**永久静音** = 麦克风是死的，用户说话一个字都收不到。
#   现在用**引用计数**：原始值只存第一次，只有最后一个窗口结束才恢复。
_MUTE_LOCK = threading.Lock()
_MUTE_SAVED = {}      # key -> (session, volume, muted)
_MUTE_DEPTH = 0


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

VK_LWIN = 0x5B
VK_H = 0x48
VK_ESCAPE = 0x1B
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


# ---------------------------------------------------------------- 焦点归还 / 输入法状态

def foreground_hwnd():
    """当前前台窗口的句柄（抢焦点之前先记住它）。"""
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def restore_foreground(hwnd):
    """把键盘焦点还给原来的窗口。

    抢了焦点就得还 —— 否则用户后面敲的字全都进了那个卡片，
    而卡片此时已经藏起来了，看起来就像「键盘失灵」。
    """
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        if not hwnd or not u.IsWindow(hwnd):
            return False
        tgt = u.GetWindowThreadProcessId(hwnd, None)
        cur = k.GetCurrentThreadId()
        if tgt:
            u.AttachThreadInput(cur, tgt, True)
        u.SetForegroundWindow(hwnd)
        if tgt:
            u.AttachThreadInput(cur, tgt, False)
        return True
    except Exception:
        return False


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", _RECT),
    ]


def ime_composing():
    """当前线程有没有输入法在组字。

    组字期间 hwndCaret 指向类名是 "IME" 的那个窗（输入法候选窗）。
    中文输入法提交时按下的那一下回车是**程序注入**的，
    但它是有意图的 —— 所以输入法在组字时要放行。
    """
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        ti = _GUIThreadInfo()
        ti.cbSize = ctypes.sizeof(ti)
        if not u.GetGUIThreadInfo(k.GetCurrentThreadId(), ctypes.byref(ti)):
            return False
        if not ti.hwndCaret:
            return False
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(ti.hwndCaret, buf, 64)
        return (buf.value or "").strip().lower() in _IME_CLASSES
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


def suppress_voice_ui(max_seconds=None, interval=0.05, on_hide=None):
    """Win+H 之后把语音输入 UI 藏起来（藏到语音会话结束为止）。

    面板那扇窗是外壳 ApplicationFrameWindow 的**子窗口**，EnumWindows 看不见，
    只能靠 EnumChildWindows(GetDesktopWindow()) 找到。
    判定：外壳托管 + **触发之后新冒出来**的小窗。
    触发前就在的那扇（输入法自己的）一个都不碰 —— 藏了中文输入就废了。

    on_hide: 每藏掉一扇窗回调一次（留痕），参数是描述串。
    """
    import threading
    u = ctypes.windll.user32
    my_pid = os.getpid()
    limit = float(max_seconds if max_seconds is not None else _VOICE_MAX_HIDE_SEC)

    # 触发前先拍一张可见窗口快照（给提示卡那条老规则用）
    visible_before = set()
    for (h, pid, cls, title) in _iter_windows():
        if u.IsWindowVisible(h):
            visible_before.add(h)

    def run():
        hidden = []
        panel = None
        panel_host = None
        end = time.time() + limit
        idle_since = None
        session_seen = False
        tick = 0
        while time.time() < end:
            tick += 1
            try:
                # 1) 语音面板：认准那扇画着蓝按钮的窗，然后一直按住藏
                #    （面板那扇窗是**复用的**，触发前就存在，所以不能靠
                #     「新冒出来」判断；靠的是「矩形里有那颗蓝按钮」）
                if panel is None:
                    panel, panel_host = _find_voice_panel_window()
                    if panel:
                        # 认出来的这扇窗**用过像素判据**（蓝按钮）—— 缓存下来。
                        # dismiss_voice_session() 要给它直接发 Esc：
                        # 认错了窗 = Esc 打到中文输入法自己的窗上。
                        global _PANEL_HWND
                        _PANEL_HWND = int(panel)
                        hidden.append((panel, panel_host))
                        d = _describe_window(panel)
                        was_vis = bool(d[3]) if d else False
                        if on_hide:
                            # was_visible=False 说明这扇窗**本来就是藏着的**
                            # （上一次抑制线程被杀留下的烂摊子）——
                            # 这次收尾会顺手把它放出来（自愈）。
                            on_hide("panel hwnd=%s host=%r was_visible=%s"
                                    % (panel, panel_host, was_vis))
                for (h, pcls) in list(hidden):
                    try:
                        if u.IsWindow(h) and u.IsWindowVisible(h):
                            u.ShowWindow(h, _SW_HIDE)
                    except Exception:
                        pass
                # 2) 其它语音 UI（提示卡等）—— 老规则，**低频**扫
                #    （EnumWindows + 取窗口标题很贵，10Hz 全量扫会把面板探测拖慢）
                if tick % 5 == 1:
                    for (h, pid, cls, title) in _iter_windows():
                        if not u.IsWindowVisible(h):
                            continue
                        if is_voice_ui(h, pid, cls, title, my_pid, h in visible_before):
                            u.ShowWindow(h, _SW_HIDE)
                            if on_hide:
                                on_hide("ui hwnd=%s cls=%r title=%r" % (h, cls, title))
            except Exception:
                pass
            # 语音会话结束就收工（会话不 Active 且静默一会儿）
            # ⚠️ 语音会话**没起来过**的时候不能急着收工 ——
            #    面板比会话晚半秒到，收早了等于没藏。
            if tick % 4 == 0:
                if _voice_session_active():
                    session_seen = True
                    idle_since = None
                else:
                    now = time.time()
                    if idle_since is None:
                        idle_since = now
                    elif session_seen:
                        if now - idle_since > _VOICE_IDLE_SEC:
                            break
                    elif now - idle_since > _VOICE_WAIT_SEC:
                        break
            time.sleep(interval)
        # 收工：把藏起来的窗放回去
        for (h, pcls) in hidden:
            try:
                if u.IsWindow(h):
                    u.ShowWindow(h, 5)   # SW_SHOW
            except Exception:
                pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 语音输入面板（实测结论）

# ⚠️ 这台机器上 Win+H 的语音面板**不是一个能枚举到的顶层窗口**，找它踩了一整天：
#   - 外壳：explorer.exe 的 ApplicationFrameWindow，rect=(640,702,160,102)
#   - 渲染层：textinputhost.exe 的 CoreWindow「Windows 输入体验」，
#     **它是外壳的子窗口**，所以 EnumWindows / FindWindowEx 都看不见；
#     只有 EnumChildWindows(GetDesktopWindow()) 能摸到（它枚举桌面下所有后代）。
#   - 藏 TextInputHost 那个**全屏** CoreWindow（1440x900，输入法宿主）**没用** ——
#     面板是另一扇小窗，实测藏它像素从 6895 → 291（面板真的没了）。
#
# 规则：只藏「触发之后新冒出来、且由外壳 ApplicationFrameWindow 托管的小窗」。
# 触发前就存在的那扇小窗（输入法自己的）**一个都不碰** —— 藏了中文输入就废了。

_VOICE_PANEL_PROC = "textinputhost.exe"
_VOICE_PANEL_CLASS = "Windows.UI.Core.CoreWindow"
_VOICE_PANEL_HOST_CLASS = "ApplicationFrameWindow"
_VOICE_PANEL_MAX_W = 700
_VOICE_PANEL_MAX_H = 500
_VOICE_MAX_HIDE_SEC = 120.0
_VOICE_IDLE_SEC = 2.0       # 语音会话静默多久就认为面板已经收工
_VOICE_WAIT_SEC = 12.0      # 语音会话一直没起来（Win+H 失败）-> 等多久就放弃


# ------------------------------------------------------- 坐标空间自校准（DPI 坑）
#
# ⚠️ 这台机器上踩过的最隐蔽的一个坑（2026-11-08 实测定位）：
#   GetWindowRect 返回的坐标**空间取决于调用进程的 DPI 感知**：
#     - DPI-unaware（普通 python.exe）        -> 逻辑坐标 1440x900
#     - per-monitor aware（PyQt5/PyInstaller）-> 物理坐标 2880x1800
#   而 ImageGrab.grab() **永远**是物理像素。
#   早先按 GetDpiForWindow/96 生乘，在 app 里等于多乘一倍，
#   截到屏幕外面去 -> 蓝按钮像素恒为 0 -> 面板永远认不出来 -> 面板永远藏不掉。
#   （源码级自检是在 DPI-unaware 的 python.exe 里跑的，所以一直是绿的 —— 同一个 bug，
#     只有打包成 app 才会犯。这正是「必须在装好的 exe 上验收」的典型例子。）
#
# 正确做法：**不猜**，用「物理屏幕尺寸 / 本进程 GetSystemMetrics 尺寸」自校准。
#   DPI-unaware：2880/1440 = 2.0（逻辑 -> 物理）
#   per-monitor：2880/2880 = 1.0（本来就是物理）
# 两种模式都实测过（蓝像素 1693 / 1678，判据一致）。

class _DEVMODEW(ctypes.Structure):
    """EnumDisplaySettingsW 要的 DEVMODEW（只要用到显示模式那几个字段）。"""
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]


def _physical_screen_size():
    """主显示器的**物理**像素尺寸（跟进程 DPI 感知无关）。"""
    try:
        dm = _DEVMODEW()
        dm.dmSize = ctypes.sizeof(_DEVMODEW)
        if ctypes.windll.user32.EnumDisplaySettingsW(None, 0, ctypes.byref(dm)):
            return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    except Exception:
        pass
    return 0, 0


_CAPTURE_SCALE = None


def _capture_scale():
    """GetWindowRect 的坐标 -> ImageGrab 物理像素 的换算系数 (sx, sy)。"""
    global _CAPTURE_SCALE
    if _CAPTURE_SCALE is None:
        sx = sy = 1.0
        try:
            pw, ph = _physical_screen_size()
            lw = int(ctypes.windll.user32.GetSystemMetrics(0))
            lh = int(ctypes.windll.user32.GetSystemMetrics(1))
            if pw > 0 and lw > 0:
                sx = pw / float(lw)
            if ph > 0 and lh > 0:
                sy = ph / float(lh)
        except Exception:
            pass
        _CAPTURE_SCALE = (sx, sy)
    return _CAPTURE_SCALE


def _describe_window(hwnd):
    """(pid, class, title, visible, rect) —— 拿不到就返回 None。"""
    try:
        u = ctypes.windll.user32
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        n = u.GetWindowTextLengthW(hwnd)
        ttl = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, ttl, n + 1)
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = _RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        return (pid.value, cls.value, ttl.value, bool(u.IsWindowVisible(hwnd)),
                r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


def _voice_panel_candidates():
    """语音输入宿主名下的小窗：[(hwnd, 外壳类名)]。

    ⚠️ 必须用 EnumChildWindows(GetDesktopWindow()) —— 面板那扇窗是外壳的**子窗口**，
    EnumWindows 只列顶层窗，根本看不到它。
    """
    out = []
    try:
        u = ctypes.windll.user32
        desktop = u.GetDesktopWindow()

        def cb(h, l):
            try:
                d = _describe_window(h)
                if not d:
                    return True
                pid, cls, ttl, vis, lx, ty, w, ht = d
                # 大小统一换算到**物理像素**再比 —— 否则 DPI-unaware 和
                # per-monitor aware 两个模式下筛出来的窗口集合不一样。
                sx, sy = _capture_scale()
                pw, ph = w * sx, ht * sy
                if (cls == _VOICE_PANEL_CLASS
                        and 0 < pw <= _VOICE_PANEL_MAX_W and 0 < ph <= _VOICE_PANEL_MAX_H
                        and _proc_name(pid) == _VOICE_PANEL_PROC):
                    parent = u.GetParent(h)
                    pcls = ""
                    if parent:
                        pd = _describe_window(parent)
                        if pd:
                            pcls = pd[1]
                    out.append((h, pcls))
            except Exception:
                pass
            return True

        u.EnumChildWindows(desktop, _ENUMPROC(cb), 0)
    except Exception:
        pass
    return out


_VOICE_BUTTON_RGB = (75, 195, 255)   # 语音面板那颗蓝色麦克风按钮的实测颜色


def _dpi_scale(hwnd):
    """窗口自己的 DPI 比例（只作参考）。**别拿它做截屏换算** —— 见 _capture_scale。"""
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi and dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _grab_region(left, top, width, height):
    """抓一块**物理**像素区域 -> PIL.Image（快路径）。

    ⚠️ 不要用 PIL.ImageGrab.grab(bbox=...)：它在 Windows 上是**抓整屏再裁**，
    2880x1800 一次 ~67ms（实测），放在 10Hz 的轮询里太重 ——
    「面板出现 -> 被藏」会拖到一秒多，用户看得见那一下。
    这里直接 GDI BitBlt 那一小块，实测 ~10ms。

    ⚠️ 屏幕 DC 的坐标**永远是物理像素**，跟进程的 DPI 感知无关 ——
    实测：DPI-unaware 进程里 BitBlt(640,702) 截到的是一片空的物理区（蓝 0），
    必须传物理坐标 (1280,1404) 才截得到面板（蓝 1678）。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return None
    try:
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        hdc = u.GetDC(0)
        mem = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w, h)
        old = g.SelectObject(mem, bmp)
        try:
            g.BitBlt(mem, 0, 0, w, h, hdc, int(left), int(top), 0x00CC0020)
            bi = _BITMAPINFOHEADER()
            bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bi.biWidth = w
            bi.biHeight = -h          # 自上而下
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            got = g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        finally:
            g.SelectObject(mem, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
            u.ReleaseDC(0, hdc)
        if not got:
            return None
        return Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
    except Exception:
        return None


def _has_mic_button(hwnd, left, top, width, height):
    """这扇窗的矩形里有没有那颗蓝色麦克风按钮（像素判据）。

    面板和输入法自己的那扇小窗**结构完全一样**（同进程、同类名、同为外壳的子窗、
    都一直可见），唯一能区分的就是**画没画那颗蓝按钮**。
    实测：面板那扇 rect 内蓝像素 ~1600，输入法那扇恒为 0。

    left/top/width/height 是 **GetWindowRect 的原始值**（调用进程坐标空间），
    内部按 _capture_scale() 换算成物理像素再截。
    """
    try:
        sx, sy = _capture_scale()
        pl, pt = int(left * sx), int(top * sy)
        pw, ph = int(width * sx), int(height * sy)
        if pw <= 0 or ph <= 0:
            return 0
        img = _grab_region(pl, pt, pw, ph)
        if img is None:
            # 兜底：ImageGrab（慢，但通用）
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab(bbox=(pl, pt, pl + pw, pt + ph)).convert("RGB")
            except Exception:
                return 0
        px = img.load()
        iw, ih = img.size
        n = 0
        for y in range(0, ih, 2):
            for x in range(0, iw, 2):
                r, g, b = px[x, y]
                if (abs(r - _VOICE_BUTTON_RGB[0]) <= 30 and
                        abs(g - _VOICE_BUTTON_RGB[1]) <= 30 and
                        abs(b - _VOICE_BUTTON_RGB[2]) <= 30):
                    n += 1
        return n
    except Exception:
        return 0


def _find_voice_panel_window():
    """找出**正在显示的**语音输入面板那扇窗 -> (hwnd, 外壳类名)。

    判据：自己矩形里画着那颗蓝色麦克风按钮（>50 个蓝像素），
    且由外壳 ApplicationFrameWindow 托管 —— 别的一律不碰。

    ⚠️ **必须要求它是可见的**（2026-11-09 踩过这个坑）：
    试过放宽成「身份对得上就认」，结果抑制线程在收尾时会把
    **Windows 自己藏着的面板强行显示出来** —— 凭空多一个空浮窗。
    面板的可见性归 Windows 管；我们只在它「正在显示」时把它藏掉。

    为什么今天日志里一条 suppress_voice_ui 都没有：面板压根没弹出来
    （hwnd=329014 一直 vis=False），所以没东西要藏 —— 这是对的，
    不是安全网坏了。真正的问题在别处（见 dismiss_voice_session）。
    """
    try:
        u = ctypes.windll.user32
        for (h, pcls) in _voice_panel_candidates():
            if pcls != _VOICE_PANEL_HOST_CLASS:
                continue
            d = _describe_window(h)
            if not d:
                continue
            _, _, _, vis, l, t, w, ht = d
            if not vis:
                continue
            if _has_mic_button(h, l, t, w, ht) > 50:
                return h, pcls
    except Exception:
        pass
    return None, None


# 认出来的面板句柄缓存 —— 免得每次问「语音还在不在」都去全桌面扫一遍
_PANEL_HWND = 0


def _voice_panel_hwnd_light():
    """轻量找面板：只按身份找（外壳托管 + 小窗），**不做像素判据**。

    「语音会话还活着吗」这种问题要问得很频繁（每次还焦点前都要问），
    不适合每问一次就截一次屏。
    """
    try:
        for (h, pcls) in _voice_panel_candidates():
            if pcls != _VOICE_PANEL_HOST_CLASS:
                continue
            return h
    except Exception:
        pass
    return 0


def _panel_hwnd_cached():
    """面板句柄（认出来就缓存）。"""
    global _PANEL_HWND
    try:
        if _PANEL_HWND and ctypes.windll.user32.IsWindow(_PANEL_HWND):
            return _PANEL_HWND
    except Exception:
        pass
    try:
        h = _voice_panel_hwnd_light()
        if h:
            _PANEL_HWND = h
    except Exception:
        pass
    return _PANEL_HWND


def dismiss_voice_session():
    """把 Windows 语音输入会话关掉（发一个 Esc），返回是否发出。

    为什么必须关：Win+H 起的语音会话会**一直按着键盘**，直到它自己超时。
    这期间用户敲的字会被语音输入吃掉 —— 哪怕焦点已经还给用户的应用了，
    表现就是「别的应用没有反馈」（2026-11-09 实测：发完 Win+H 0.5 秒时
    语音音频会话还是 Active）。

    ⚠️ **只在会话真的活着的时候发**：会话已经结束时再补一个 Esc，
    会变成在用户的应用里乱按一下。

    ⚠️⚠️ 2026-11-09 实测：**全局 Esc 根本到不了 Windows 的语音面板** ——
    Esc 是发给**当前焦点窗口**的，而我们为了收字**把焦点抢到了自己的卡片上**，
    这一发 Esc 被自己的输入框吃了（`_cancel()` 里 editing 已经是 False，直接忽略）。
    结果：会话还在活着，键盘还被系统按着，只能干等它自己超时
    （实测 2.5 秒 —— 而那 2.5 秒是**阻塞在 GUI 线程上**的，实测「按回车就卡一会」）。
    -> 现在**直接给面板窗口发 Esc**（PostMessage，不需要焦点），
       全局 Esc 只当兜底（面板不认消息时才用）。
    """
    try:
        if not _voice_session_active():
            return False
    except Exception:
        return False
    u = ctypes.windll.user32
    sent = False
    try:
        h = _panel_hwnd_cached()
        if h:
            u.PostMessageW(ctypes.c_void_p(int(h)), 0x0100, 0x1B, 0)   # WM_KEYDOWN VK_ESCAPE
            u.PostMessageW(ctypes.c_void_p(int(h)), 0x0101, 0x1B, 0)   # WM_KEYUP
            sent = True
    except Exception:
        pass
    try:
        u.keybd_event(VK_ESCAPE, 0, 0, 0)
        time.sleep(0.03)
        u.keybd_event(VK_ESCAPE, 0, KEYUP, 0)
        sent = True
    except Exception:
        pass
    return sent


def voice_ui_active():
    """Windows 语音输入现在是不是还「开着」（面板在，或者语音会话还活着）。

    用它守住一件事：**别在语音会话还活着的时候把键盘还给用户的应用**。
    语音会话期间键盘是被系统语音输入按着的，这时候把焦点还回去，
    用户敲的字会被悄悄吃掉 —— 表现就是「别的应用没反馈」。
    """
    try:
        if _voice_session_active():
            return True
    except Exception:
        pass
    try:
        h = _panel_hwnd_cached()
        if h and ctypes.windll.user32.IsWindowVisible(h):
            return True
    except Exception:
        pass
    return False


def _voice_session_active():
    """语音输入宿主的音频会话是不是 Active（= 面板还开着）。查不到就当 False。"""
    try:
        for role in (_ROLE_MULTIMEDIA, _ROLE_CONSOLE):
            for s in list_audio_sessions(role):
                try:
                    if s.owner == _VOICE_PANEL_PROC and s.state == 1:
                        return True
                finally:
                    s.free()
    except Exception:
        pass
    return False


def voice_capture_muted():
    """语音输入宿主的音频会话现在是不是**还压着**（静音窗口没结束）。

    为什么必须有这个（2026-11-09 实测踩出来的）：
      - 那声「叮」是**面板真正打开**（= 开始录音）时播的，按键后 1~2 秒；
      - 静音窗口是用来盖住它的，但窗口一开，面板还在**采集**（静音连采集一起压），
        也就是说**静音没解除之前麦克风是不收音的**。
    所以「麦克风已就绪」这句话不能只看会话活没活 —— 还得看静音解没解。
    静音没解就说「就绪」= 用户一开口就没字。
    """
    try:
        for role in (_ROLE_MULTIMEDIA, _ROLE_CONSOLE):
            for s in list_audio_sessions(role):
                try:
                    if s.owner == _VOICE_PANEL_PROC and s.muted:
                        return True
                finally:
                    s.free()
    except Exception:
        pass
    return False


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
        self.state = None        # 0=inactive 1=active 2=inactive-but-open
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
        try:
            st = ctypes.c_int()
            if _vt(self.ctrl, 3, ctypes.c_long,
                   [ctypes.POINTER(ctypes.c_int)])(self.ctrl, ctypes.byref(st)) == 0:
                self.state = st.value
        except Exception:
            pass
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

    ⚠️⚠️ 2026-11-09 22:05 修：**窗口重叠会把麦克风永久压死**。
    旧实现每次都存一次「当前状态」、到期再存回去：
      窗口 A：存 (1.0, 未静音) -> 压成静音
      窗口 B（与 A 重叠）：存到的已经是静音态 -> 到期恢复成静音态
      结果：语音输入宿主的会话**永久静音** = 麦克风是死的，
      用户说话一个字都收不到（实测 2026-11-09 20:41 连按两次 AI 键就中招）。
    现在：**引用计数 + 原始值只存第一次**，最后一个窗口结束才恢复。
    """
    global _MUTE_DEPTH
    my_pid = os.getpid()

    with _MUTE_LOCK:
        _MUTE_DEPTH += 1

    def run():
        global _MUTE_DEPTH
        _com_init()
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
                                # 已经被我们压过（上一轮窗口还没结束）-> 不要再存一次
                                with _MUTE_LOCK:
                                    if k in _MUTE_SAVED:
                                        s.free()
                                        continue
                                    vol, muted = s.volume, s.muted
                                    _MUTE_SAVED[k] = (s, vol, muted)
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
            with _MUTE_LOCK:
                _MUTE_DEPTH = max(0, _MUTE_DEPTH - 1)
                last = (_MUTE_DEPTH == 0)
                items = list(_MUTE_SAVED.values()) if last else []
                if last:
                    _MUTE_SAVED.clear()
            for s, vol, muted in items:
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
            if last:
                # 兜底：会话对象可能已经失效（Windows 重建了会话），恢复会失败，
                # 而失败被上面的 except 吞掉 -> 留下一个永久静音的麦克风。
                # 直接按名字找回来解掉。
                try:
                    if unstick_voice_capture():
                        if on_change:
                            on_change("unstuck a stuck-muted voice session")
                except Exception:
                    pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def voice_mute_active():
    """我们自己的静音窗口现在是不是还开着（区分「我们压的」和「卡死的」）。"""
    try:
        with _MUTE_LOCK:
            return _MUTE_DEPTH > 0
    except Exception:
        return False


def unstick_voice_capture():
    """把卡死的静音解开：麦克风被静音 = 用户说话一个字都收不到。

    2026-11-09 实测踩到过：静音窗口重叠会让语音输入宿主的会话永久静音，
    而恢复失败被 except 吞掉，日志里看不出来 —— 表现是「按 AI 键没反应」。
    """
    ok = False
    for role in (_ROLE_MULTIMEDIA, _ROLE_CONSOLE):
        try:
            for s in list_audio_sessions(role):
                try:
                    if s.owner == _VOICE_PANEL_PROC and s.muted:
                        s.set_volume(1.0)
                        s.set_mute(False)
                        ok = True
                except Exception:
                    pass
                finally:
                    s.free()
        except Exception:
            pass
    return ok
