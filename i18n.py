# -*- coding: utf-8 -*-
"""中英双语：界面语言 / 回复语言 / 听写语言，三层分开管。

为什么要分三层：这三件事经常要分开选 —— 面板想看中文，但助手想用英文回答；
或者界面是中文，但用户说的是英文、助手得听懂。揉在一起就会出现
「为了说英文，把面板也切成英文」这种别扭情况。

  1) 界面语言   ui_lang      = zh | en
     管：设置面板的标签/提示/按钮、覆盖层上的状态字（聆听中/思考中/说话中）
  2) 回复语言   reply_lang   = zh | en | both
     管：助手回答用哪种语言；both = 中英对照（先中文后英文，两段都显示）
  3) 听写语言   stt.language = auto | zh-CN | en-US
     管：识别用哪种；auto = 上次成功的语言优先，这次没听清就换另一种重试

⚠️ system_prompt 保持**语言中性**：基础提示词只描述「你是谁、怎么说话」，
   语言规则由 LANG_RULES 按 reply_lang 追加 —— 这样用户自己写的提示词
   在三种回复语言下都照样生效，不会被面板偷偷改掉。

本模块不 import voice / llm / siri_orb —— 谁都能 import 它，不会有循环依赖。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ZH = "zh"
EN = "en"
BOTH = "both"

UI_LANGS = ("zh", "en")
REPLY_LANGS = ("zh", "en", "both")
STT_LANGS = ("auto", "zh-CN", "en-US")


# ---------------------------------------------------------------- 界面文案
# key -> (中文, English)
STRINGS = {
    # ---------------- 设置面板
    "settings.title": ("Siri-OS 设置", "Siri-OS Settings"),
    "settings.subtitle": ("改完自动保存 · 常驻助手 1 秒内自动生效（不用重启）",
                          "Changes save automatically · the resident assistant applies them within a second (no restart)"),
    "settings.resident.checking": ("常驻助手：检查中…", "Assistant: checking…"),
    "settings.resident.running": ("常驻助手：运行中（pid %d）", "Assistant: running (pid %d)"),
    "settings.resident.stopped": ("常驻助手：未运行（改的设置在它下次启动时生效）",
                                  "Assistant: not running (changes apply when it starts)"),
    "settings.btn.demo": ("试一下", "Try it"),
    "settings.btn.demo.tip": ("在屏幕上跑一次扩散 + 跑马灯（不碰麦克风、不问模型）",
                              "Run one diffusion + marquee on screen (no microphone, no model)"),
    "settings.btn.keys": ("打开密钥库", "Open key store"),
    "settings.btn.keys.tip": ("换一个后端的 API key（不经过聊天记录）",
                              "Change a backend API key (never pasted into chat)"),
    "settings.btn.reset": ("恢复推荐配置", "Reset to recommended"),
    "settings.btn.reset.tip": ("把所有旋钮调回已验收的那一套参数（接口密钥不受影响）",
                               "Put every knob back to the verified parameter set (API keys are untouched)"),
    "settings.secret.have": ("已保存（%d 位），留空表示不修改", "Saved (%d chars). Leave blank to keep it."),
    "settings.secret.saved": ("%s 的接口密钥已保存，正在验活…",
                              "API key for %s saved, checking it..."),
    "settings.autostart.on": ("开机自启已开启", "Autostart enabled"),
    "settings.autostart.off": ("开机自启已关闭", "Autostart disabled"),
    "settings.autostart.failed": ("开机自启设置失败：%s", "Could not change autostart: %s"),
    "settings.btn.close": ("关闭", "Close"),
    "settings.saved": ("✓ 已保存 · 常驻助手 1 秒内自动应用",
                       "✓ Saved · the assistant applies it within a second"),
    "settings.save_failed": ("✗ 保存失败：%s", "✗ Save failed: %s"),
    "settings.reset_done": ("✓ 已恢复到已验收的那套默认参数",
                            "✓ Restored the verified default parameters"),
    "settings.demo.ok": ("→ 已让常驻助手（pid %d）跑一次扩散 + 跑马灯",
                         "→ Asked the assistant (pid %d) to run a diffusion + marquee"),
    "settings.demo.no_resident": ("→ 请求已记录，但常驻助手没在运行 —— 下次启动会看到效果",
                                  "→ Request recorded, but the assistant isn't running — you'll see it next launch"),
    "settings.demo.failed": ("✗ 触发失败：%s", "✗ Trigger failed: %s"),
    "settings.keys.opened": ("→ 已打开密钥窗口，在里面选后端并粘贴 key",
                             "→ Key window opened — pick a backend and paste the key"),
    "settings.keys.failed": ("✗ 打不开密钥窗口：%s", "✗ Cannot open the key window: %s"),
    "settings.backends.title": ("后端可用性（真实发一次请求）",
                                "Backend availability (sends a real request)"),
    "settings.backends.checking": ("%s：检测中…", "%s: checking…"),
    "settings.backends.recheck": ("重新检测", "Re-check"),
    "settings.order.up": ("上移", "Move up"),
    "settings.order.down": ("下移", "Move down"),
    "settings.backend.no_key": ("未配置 key", "no key configured"),
    "settings.backend.balance": ("余额不足（402）· 去 platform.minimaxi.com 充值",
                                 "out of credit (402) · top up at platform.minimaxi.com"),
    "settings.backend.auth": ("key 无效（401/403）", "invalid key (401/403)"),
    "settings.backend.ok": ("可用 · ", "available · "),
    "settings.backend.failed": ("失败：", "failed: "),
    "settings.import_error": ("无法导入 llm：%s", "cannot import llm: %s"),

    # ---------------- 覆盖层 / 语音对话框
    "state.listening": ("聆听中", "Listening"),
    "state.thinking": ("思考中", "Thinking"),
    "state.speaking": ("说话中", "Speaking"),
    "card.hint": ("点击可编辑 · Enter 发送", "Click to edit · Enter to send"),
    "reply.thinking": ("思考中……", "Thinking…"),
    "reply.not_heard": ("没听清，再说一次？", "Didn't catch that — say again?"),
    "reply.mic_crash": ("麦克风异常：听写进程崩溃（rc=%s）",
                        "Microphone error: the recognition process crashed (rc=%s)"),
    "reply.error": ("出错了：%s", "Something went wrong: %s"),
    "reply.model_down": ("模型没连上。运行 python set_key.py 填一下 key 就能用。%s",
                         "The model isn't reachable. Run: python set_key.py to store a key. %s"),

    # ---------------- 托盘
    "tray.demo": ("演示一次", "Demo"),
    "tray.settings": ("设置…", "Settings…"),
    "tray.quit": ("退出", "Quit"),
    "tray.tooltip": ("Siri-OS 桌面语音助手", "Siri-OS desktop voice assistant"),
}


# ------------------------------------------------------------ 覆盖层状态字
STATE_TEXT = {
    "idle": ("", ""),
    "listening": ("聆听中", "Listening"),
    "thinking": ("思考中", "Thinking"),
    "speaking": ("说话中", "Speaking"),
}


# ------------------------------------------- 回复语言规则（追加到 system_prompt）
LANG_RULES = {
    "zh": "用中文回答。",
    "en": "Reply in English.",
    "both": ("回答两次：先用中文说一遍，再用英文说一遍，两段之间空一行；"
             "中文段落以「中文：」开头，英文段落以「English:」开头。"),
}


# ------------------------------------------------------------ 小工具
def _is_en(lang):
    return str(lang or "").lower() == EN


def t(lang, key, default=None):
    """取一条界面文案。lang=zh 取中文，其它取英文。"""
    item = STRINGS.get(key)
    if item is None:
        return key if default is None else default
    return item[1] if _is_en(lang) else item[0]


def state_text(lang, state):
    item = STATE_TEXT.get(state) or ("", "")
    return item[1] if _is_en(lang) else item[0]


def ui_lang(cfg):
    v = str((cfg or {}).get("ui_lang") or ZH).lower()
    return EN if v == EN else ZH


def reply_lang(cfg):
    v = str((cfg or {}).get("reply_lang") or ZH).lower()
    return v if v in REPLY_LANGS else ZH


def has_cjk(s):
    for ch in (s or ""):
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            return True
    return False


def split_bilingual(text):
    """把一段可能中英混排的文本拆成 (中文段, 英文段)。

    判据用「这一行里有没有汉字」—— 双语回复的约定是两段各占一行，
    所以按行拆比按句拆稳。
    """
    zh, en = [], []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if has_cjk(s):
            zh.append(s)
        else:
            en.append(s)
    return "\n".join(zh), "\n".join(en)


def build_system_prompt(cfg):
    """基础提示词（语言中性）+ 按回复语言追加的语言规则。"""
    cfg = cfg or {}
    base = str(cfg.get("system_prompt") or "").strip()
    rule = LANG_RULES.get(reply_lang(cfg), LANG_RULES[ZH])
    if base and rule:
        return base + "\n\n" + rule
    return base or rule


# ------------------------------------------------- 听写语言：上次成功的那个
_LAST_STT = {"lang": None}


def last_stt_lang():
    """上次听写成功的识别器语言（进程内记忆）。"""
    return _LAST_STT.get("lang")


def set_last_stt_lang(lang):
    if lang:
        _LAST_STT["lang"] = lang


def stt_langs(cfg):
    """按配置返回**有序**的听写语言列表。

    auto → 上次成功的语言优先，另一个排第二（没听清时换一种再试）。
    其它 → 就那一种。
    """
    stt = (cfg or {}).get("stt") or {}
    v = str(stt.get("language") or "zh-CN").strip()
    if v.lower() == "auto":
        first = last_stt_lang() or "zh-CN"
        second = "en-US" if first == "zh-CN" else "zh-CN"
        return [first, second]
    return [v]
