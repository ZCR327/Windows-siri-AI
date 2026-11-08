# -*- coding: utf-8 -*-
"""语音链路：SAPI 离线听写 + SAPI 语音合成 + 本地系统指令（中英双语）。

🈳 双语（2026-11-07）：
  - 本地指令（几点/几号/静音/搜索/打开程序）会按 lang 出中英两种说法；
  - 播报时按当前声线的语言挑一段念：中文声线念中文段，英文声线念英文段，
    本机装了 Huihui（中文）/ Zira、David（英文），所以能自动切。
"""
import os
import sys
import time
import ctypes
import datetime
import threading
import urllib.parse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import i18n

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

# listen.ps1 是**只读资源**：打包后要在解包目录里找（bundle_dir）
# 临时结果是**可写数据**：打包后写在 exe 同级目录（data_dir）
LISTEN_PS1 = os.path.join((pathmod.bundle_dir() if pathmod else HERE), "listen.ps1")
TMP_DIR = os.path.join((pathmod.data_dir() if pathmod else HERE), "tmp")

try:
    import win32com.client
    import pythoncom
except Exception:  # pragma: no cover
    win32com.client = None
    pythoncom = None


# 声线语言判定用的关键词（描述里出现就算那个语言）
_ZH_VOICE_TOKENS = ("chinese", "huihui", "kangkang", "xiaoxiao", "yunyang", "中文")
_EN_VOICE_TOKENS = ("english", "zira", "david", "英文")


def _ensure_tmp():
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
    except Exception:
        pass


def listen_once(timeout_sec=10, language="zh-CN", on_level=None, info=None):
    """阻塞式听写一次，返回识别文本（没听清返回空串）。

    on_level: 可选回调。识别过程中 listen.ps1 会实时吐出 "LVL <t> <电平0-100>"，
    这里换算成 0..1 逐个回调 —— 用来驱动边光随说话节奏向内收缩。

    info: 可选字典，回填诊断信息 {"rc": 退出码, "file_written": bool, "crashed": bool}。
    ⚠️ 听写脚本在麦克风异常时会 AccessViolation 直接崩掉、根本来不及写结果文件，
    所以开跑前必须先删掉上一次的结果文件 —— 否则会把上次的话当成这次听到的。
    """
    _ensure_tmp()
    out = os.path.join(TMP_DIR, "stt_last.txt")
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", LISTEN_PS1,
           "-OutFile", out, "-TimeoutSec", str(int(timeout_sec)), "-Language", language]
    try:
        # ⚠️ 必须 CREATE_NO_WINDOW：常驻进程跑在 pythonw.exe 下（自身没有控制台），
        # Windows 会给 powershell 新建一个控制台窗口 —— 每次按键都闪一个黑框。
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return ""

    def _reader():
        try:
            for raw in p.stdout:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("LVL "):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    v = float(parts[2]) / 100.0
                except Exception:
                    continue
                if on_level is not None:
                    try:
                        on_level(max(0.0, min(1.0, v)))
                    except Exception:
                        pass
        except Exception:
            pass

    th = threading.Thread(target=_reader, daemon=True)
    th.start()
    # ⚠️ 不能用 communicate()：它会把 stdout 一起读走，跟上面的读取线程抢管道
    #（实测只收到 1 个采样）。改成等进程自己退出，管道由读取线程排空。
    try:
        p.wait(timeout=timeout_sec + 12)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
            p.wait(timeout=3)
        except Exception:
            pass
    th.join(timeout=2)
    text = ""
    wrote = False
    try:
        with open(out, "r", encoding="utf-8") as f:
            text = f.read().strip()
        wrote = True
    except Exception:
        text = ""
    rc = p.returncode
    if info is not None:
        try:
            info["rc"] = rc
            info["file_written"] = wrote
            info["crashed"] = bool((rc is not None and rc != 0) or (not wrote))
        except Exception:
            pass
    return text


class Speaker(object):
    """SAPI TTS。默认中文声线，但会按回复语言自动切到英文声线。"""

    def __init__(self, voice_hint="Huihui", rate=0):
        self.voice_hint = voice_hint
        self.rate = rate
        self._voice = None

    def _ensure(self):
        if self._voice is not None:
            return self._voice
        if win32com.client is None:
            return None
        pythoncom.CoInitialize()
        v = win32com.client.Dispatch("SAPI.SpVoice")
        try:
            if self.voice_hint:
                for tok in v.GetVoices():
                    desc = tok.GetDescription()
                    if self.voice_hint.lower() in desc.lower():
                        v.Voice = tok
                        break
        except Exception:
            pass
        try:
            v.Rate = self.rate
        except Exception:
            pass
        self._voice = v
        return v

    def speak(self, text):
        if not text:
            return
        try:
            v = self._ensure()
            if v is None:
                return
            v.Speak(text, 0)  # 0 = 同步播报
        except Exception:
            pass

    # ------------------------------------------------------------ 双语播报
    def _desc(self):
        try:
            v = self._ensure()
            if v is None:
                return ""
            return (v.Voice.GetDescription() or "")
        except Exception:
            return ""

    def _lang_of(self, desc):
        d = (desc or "").lower()
        if any(t in d for t in _ZH_VOICE_TOKENS):
            return "zh"
        if any(t in d for t in _EN_VOICE_TOKENS):
            return "en"
        return ""

    def _pick_by_lang(self, want):
        """切到匹配语言的声线（本机中英文声线都装了）。"""
        try:
            v = self._ensure()
            if v is None:
                return False
            toks = _ZH_VOICE_TOKENS if want == "zh" else _EN_VOICE_TOKENS
            for tok in v.GetVoices():
                d = (tok.GetDescription() or "").lower()
                if any(t in d for t in toks):
                    v.Voice = tok
                    return True
            return False
        except Exception:
            return False

    def speak_localized(self, text, lang="zh"):
        """按回复语言播报。

        lang=zh   -> 用中文声线念中文段
        lang=en   -> 用英文声线念英文段
        lang=both -> 不换声线，按当前声线的语言挑一段念（两段都显示在卡片上）
        """
        if not text:
            return
        zh, en = i18n.split_bilingual(text)
        want = (lang or "zh").lower()
        if want == "both":
            want = self._lang_of(self._desc()) or "zh"
        if want in ("zh", "en"):
            cur = self._lang_of(self._desc())
            if cur and cur != want:
                if self._pick_by_lang(want):
                    cur = want
            body = zh if want == "zh" else en
            if not body.strip():
                body = text
            self.speak(body)
            return
        self.speak(text)

    def describe(self):
        try:
            v = self._ensure()
            names = []
            for tok in v.GetVoices():
                names.append(tok.GetDescription())
            return names
        except Exception:
            return []


def _volume(vk):
    try:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
    except Exception:
        pass


# 能打开的程序：中文名 / 英文名 / 目标
APPS = [
    {"zh": "浏览器", "en": "browser", "target": "https://www.bing.com"},
    {"zh": "计算器", "en": "calculator", "target": "calc.exe"},
    {"zh": "记事本", "en": "notepad", "target": "notepad.exe"},
    {"zh": "画图", "en": "paint", "target": "mspaint.exe"},
    {"zh": "任务管理器", "en": "task manager", "target": "taskmgr.exe"},
    {"zh": "终端", "en": "terminal", "target": "wt.exe"},
    {"zh": "资源管理器", "en": "file explorer", "target": "explorer.exe"},
    {"zh": "命令行", "en": "command prompt", "target": "cmd.exe"},
]

WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]


def _detect_zh(t):
    """中文指令识别 —— 关键词与老版本保持一致。"""
    if any(k in t for k in ("几点", "现在时间", "什么时间", "报时")):
        return {"kind": "time"}
    if any(k in t for k in ("今天几号", "今天日期", "几号了", "星期几")):
        return {"kind": "date"}
    if "静音" in t or "关闭声音" in t:
        return {"kind": "mute"}
    if any(k in t for k in ("音量大", "声音大", "大声点", "音量调大", "音量加", "响一点")):
        return {"kind": "vol_up"}
    if any(k in t for k in ("音量小", "声音小", "小声点", "音量调小", "音量减", "轻一点")):
        return {"kind": "vol_down"}
    if "搜索" in t:
        q = t.split("搜索", 1)[-1].strip("的 一下 ")
        if q:
            return {"kind": "search", "q": q}
    if "打开" in t or "启动" in t:
        for app in APPS:
            if app["zh"] in t:
                return {"kind": "open", "app": app}
    return None


def _detect_en(t):
    """英文指令识别（t 已小写）。"""
    if any(k in t for k in ("what time", "the time is", "current time",
                            "time now", "tell me the time")):
        return {"kind": "time"}
    if any(k in t for k in ("what date", "what's the date", "whats the date",
                            "today's date", "what day is it", "day of the week")):
        return {"kind": "date"}
    if "mute" in t or "silence" in t or "stop the sound" in t:
        return {"kind": "mute"}
    if any(k in t for k in ("volume up", "turn up the volume", "louder",
                            "raise the volume", "increase the volume")):
        return {"kind": "vol_up"}
    if any(k in t for k in ("volume down", "turn down the volume", "quieter",
                            "lower the volume", "decrease the volume")):
        return {"kind": "vol_down"}
    if "search" in t or "google" in t:
        q = t
        for sep in ("search for", "search", "google"):
            if sep in q:
                q = q.split(sep, 1)[1]
                break
        q = q.strip(" ?.,!")
        if q:
            return {"kind": "search", "q": q}
    if "open" in t or "launch" in t or "start" in t:
        for app in APPS:
            if app["en"] in t:
                return {"kind": "open", "app": app}
    return None


def _perform(cmd):
    """执行动作（只做一次 —— 中英对照时两段文案共享同一次动作）。"""
    kind = cmd.get("kind")
    if kind == "mute":
        _volume(0xAD)
        return True
    if kind == "vol_up":
        _volume(0xAF)
        return True
    if kind == "vol_down":
        _volume(0xAE)
        return True
    if kind == "search":
        try:
            os.startfile("https://www.bing.com/search?q="
                         + urllib.parse.quote(cmd.get("q") or ""))
            return True
        except Exception:
            return False
    if kind == "open":
        try:
            os.startfile(cmd["app"]["target"])
            return True
        except Exception:
            return False
    return True


def _format(cmd, lang, now):
    kind = cmd.get("kind")
    en = (lang == "en")
    if kind == "time":
        if en:
            h = now.hour % 12 or 12
            return "It's %d:%02d %s." % (h, now.minute, "AM" if now.hour < 12 else "PM")
        return "现在 %d 点 %02d 分。" % (now.hour, now.minute)
    if kind == "date":
        if en:
            return "Today is %s, %s %d, %d." % (
                WEEKDAYS_EN[now.weekday()], MONTHS_EN[now.month - 1],
                now.day, now.year)
        return "今天是 %d 年 %d 月 %d 日，星期%s。" % (
            now.year, now.month, now.day, "一二三四五六日"[now.weekday()])
    if kind == "mute":
        return "Muted." if en else "静音了。"
    if kind == "vol_up":
        return "Turned the volume up." if en else "调大了。"
    if kind == "vol_down":
        return "Turned the volume down." if en else "调小了。"
    if kind == "search":
        q = cmd.get("q") or ""
        return ('Searched for "%s".' % q) if en else ("已经帮你搜「%s」。" % q)
    if kind == "open":
        app = cmd["app"]
        return ("Opened %s." % app["en"]) if en else ("已经打开%s。" % app["zh"])
    return None


def try_local_command(text, lang="zh"):
    """能本地确定性处理的指令直接处理，返回回复；否则返回 None 交给 LLM。

    lang: zh | en | both
      - zh   ：只认中文指令，答中文
      - en   ：只认英文指令，答英文
      - both ：中英都认，答「中文：… / English: …」两段
    """
    if not text:
        return None
    t = (text or "").strip()
    lang = (lang or "zh").lower()
    now = datetime.datetime.now()

    if lang == "en":
        cmd = _detect_en(t.lower())
    elif lang == "both":
        cmd = _detect_zh(t) or _detect_en(t.lower())
    else:
        cmd = _detect_zh(t)
    if not cmd:
        return None

    if not _perform(cmd):
        if cmd.get("kind") == "open":
            app = cmd["app"]
            return ("Couldn't open %s." % app["en"]) if lang == "en" \
                else ("没能打开%s。" % app["zh"])
        return "That didn't work." if lang == "en" else "没成功。"

    if lang == "both":
        parts = []
        zh = _format(cmd, "zh", now)
        en = _format(cmd, "en", now)
        if zh:
            parts.append("中文：" + zh)
        if en:
            parts.append("English: " + en)
        return "\n\n".join(parts)
    return _format(cmd, lang, now)
