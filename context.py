# -*- coding: utf-8 -*-
"""上下文：让助手记得上一句，也知道此刻电脑上有什么。

两块，用户都要：

  1) **对话上下文** —— 最近几轮问答（用户一句 + 助手一句成对）。
     「把它改短一点」「上面那个文件」这种指代才接得上。
     只存在**本进程内存里**：不写文件、不进 config.json、关掉程序就没了。

  2) **电脑状态上下文** —— 当前前台窗口、打开的窗口清单、屏幕分辨率、
     工作目录、已开启的能力。每次请求都带上，模型不用每次问「我现在开着什么」。
     （真要看屏幕长什么样，模型可以自己调 screenshot 工具 —— 那要花一次截图，
       所以不默认每轮都截，只给文字摘要 + 工具入口。）

⚠️ Anthropic 要求 user/assistant **必须交替**，而上下文拼接后可能出现
   连续两条 user —— 所以发给模型前一定要过一遍 normalize_roles()。
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import i18n
except Exception:      # pragma: no cover
    i18n = None

try:
    import tools as toolmod
except Exception:      # pragma: no cover
    toolmod = None

NL = chr(10)
NL2 = NL + NL


# ------------------------------------------------------------------ 开关
def _ctx(cfg):
    return ((cfg or {}).get("ctx") or {})


def memory_enabled(cfg):
    """要不要记住前几轮对话。"""
    return bool(_ctx(cfg).get("enabled", True))


def sysinfo_enabled(cfg):
    """要不要带上电脑状态。"""
    return bool(_ctx(cfg).get("sysinfo", True))


def history_turns(cfg):
    """记住几轮（1 轮 = 用户一句 + 助手一句）。"""
    try:
        n = int(_ctx(cfg).get("history_turns") or 6)
    except Exception:
        n = 6
    return max(0, min(20, n))


# ------------------------------------------------------------ 电脑状态
def _foreground():
    """当前前台窗口标题。"""
    try:
        import win32gui
        h = win32gui.GetForegroundWindow()
        return (win32gui.GetWindowText(h) or "") if h else ""
    except Exception:
        return ""


def _windows(limit=10):
    """当前可见窗口的标题（取前几个）。"""
    try:
        import win32gui
    except Exception:
        return []
    out = []
    try:
        def cb(hwnd, _):
            if len(out) >= limit:
                return
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                t = (win32gui.GetWindowText(hwnd) or "").strip()
                if t:
                    out.append(t[:60])
            except Exception:
                pass
        win32gui.EnumWindows(cb, None)
    except Exception:
        return out
    return out


def _screen_size():
    try:
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 0, 0


def system_context(cfg):
    """电脑状态块（拼进系统提示词）。拿不到东西就返回空串。"""
    if not sysinfo_enabled(cfg):
        return ""
    lines = ["[电脑状态]"]
    fg = _foreground()
    if fg:
        lines.append("当前前台窗口：" + fg[:80])
    wins = _windows(10)
    if wins:
        lines.append("打开的窗口：" + "、".join(wins))
    w, h = _screen_size()
    if w and h:
        lines.append("屏幕分辨率：%dx%d（要看屏幕具体样子可以调 screenshot 工具）" % (w, h))
    if toolmod is not None:
        try:
            lines.append("工作目录：" + toolmod.work_dir(cfg))
            cats = toolmod.categories_in_use(cfg)
            if cats:
                lines.append("已开启的能力：" + "、".join(cats))
        except Exception:
            pass
    if len(lines) <= 1:
        return ""
    return NL.join(lines)


# ------------------------------------------------------------ 对话记忆
class Conversation(object):
    """最近几轮问答。助手在 QThread 里跑，所以这里要加锁。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._msgs = []          # [{"role": "user"/"assistant", "content": str}]
        self._hard_cap = 40      # 内部硬上限（条），history() 再按配置裁

    def clear(self):
        with self._lock:
            self._msgs = []

    def add(self, role, content):
        text = (content or "").strip()
        if not text:
            return
        with self._lock:
            self._msgs.append({"role": role, "content": text})
            if len(self._msgs) > self._hard_cap:
                self._msgs = self._msgs[-self._hard_cap:]

    def add_turn(self, user_text, assistant_text):
        self.add("user", user_text)
        self.add("assistant", assistant_text)

    def history(self, max_turns=6):
        """最近 max_turns 轮 —— 返回 [{role, content}, ...]。

        裁剪规则：从后往前取 2*max_turns 条，再丢掉开头那些不是 user 的
        （上一轮助手的话如果没配上用户那句，发给模型会变成「assistant 在自说自话」）。
        """
        with self._lock:
            msgs = list(self._msgs)
        if max_turns <= 0:
            return []
        chunk = msgs[-(max_turns * 2):]
        while chunk and chunk[0].get("role") != "user":
            chunk = chunk[1:]
        return [{"role": m.get("role"), "content": m.get("content")} for m in chunk]


_CONV = Conversation()


def get_conversation():
    """全局那一份对话记忆（整个进程共用）。"""
    return _CONV


# 清上下文的说法（本地指令，不经过模型）
CLEAR_WORDS = (
    "清空上下文", "清除上下文", "清掉上下文", "重新开始", "忘掉刚才",
    "忘记刚才", "忘掉前面", "clear context", "forget that", "start over",
)


def is_clear_command(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    for w in CLEAR_WORDS:
        if w in t:
            return True
    return False


# ------------------------------------------------------------ 消息整形
def normalize_roles(messages):
    """把**连续同角色**的消息合并。

    Anthropic 的 /messages 要求 user / assistant 必须交替，
    而「历史 + 当前问题」拼起来可能出现连续两条 user —— 不合并会被服务端拒。
    OpenAI 不强制，但合并了也没坏处。
    """
    out = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if (out and out[-1].get("role") == role
                and isinstance(out[-1].get("content"), str)
                and isinstance(content, str)):
            out[-1] = {"role": role,
                       "content": out[-1]["content"] + NL2 + content}
        else:
            out.append(dict(m))
    return out


def compose_system(cfg, extra=""):
    """系统提示词 = 用户写的提示词 + 语言规则 + 电脑状态 + （能力说明）。"""
    base = ""
    if i18n is not None:
        try:
            base = i18n.build_system_prompt(cfg)
        except Exception:
            base = ""
    parts = [p for p in (base, system_context(cfg), extra) if p]
    return NL2.join(parts)
