# -*- coding: utf-8 -*-
"""配置读写层：设置面板 + 常驻助手共用的唯一真源。

为什么要单独一个模块：
  - DEFAULTS 只写一份（siri_orb.py 也从这里取），避免两边默认值漂移；
  - SPEC 是「每个旋钮长什么样」的**数据描述** —— 设置窗口照着它自动生成界面，
    以后加一个可调项只要往 SPEC 里加一行，不用再手写一堆控件代码；
  - 写入走「临时文件 + 原子替换」，常驻助手正好在读的时候不会读到半个文件
    （它每 0.5 秒按 mtime 热重载）。

⚠️ DEFAULTS 是「已验收的那一套参数」，恢复默认就是回到这个状态。

🈳 中英双语（2026-11-07）：每个 SPEC 条目多了 en_label / en_hint / en_unit，
   界面切成英文时整块面板（含页签名、提示、单位、下拉选项）都跟着变。
   中文是主语言，英文是译文 —— 加新旋钮时三个字段都要补。
"""
import io
import json
import os
import tempfile

try:
    import providers as prov
except Exception:      # pragma: no cover
    prov = None

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

# 打包成 exe：配置和日志必须写在 **exe 同级目录**（解包目录是只读的，写不进去）
HERE = (pathmod.data_dir() if pathmod else os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
TRIGGER_FILE = os.path.join(LOG_DIR, "demo.request")
PID_FILE = os.path.join(LOG_DIR, "siri_orb.pid")

# 便捷：往触发文件里写一行时间戳（设置面板 -> 常驻助手 的跨进程通道）
def fire_trigger(text=None):
    """请求常驻助手跑一次扩散+跑马灯演示（不碰麦克风）。

    带 text 时：第一行时间戳、第二行问题 —— 常驻助手会走**完整回复链路**
    （模型 -> 播报 -> 卡片显示）。麦克风坏着的时候，这是唯一能在屏幕上
    看到完整回复的办法，也是双语链路的验收手段。
    """
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    payload = "%f\n" % __import__("time").time()
    if text:
        payload += str(text).replace("\r\n", " ").replace("\n", " ").strip() + "\n"
    with io.open(TRIGGER_FILE, "w", encoding="utf-8") as f:
        f.write(payload)
    return True


def _registry():
    """后端注册表（providers.py）；拿不到就退回空表。"""
    if prov is not None:
        return prov.PROVIDERS
    return {}


def _llm_defaults():
    """llm 段的默认值 —— **按注册表自动生成**。

    加一家新厂商只需要在 providers.py 里加一行，这里自动多出
    model / base_url / api_key 三个默认值 + 一个 enabled 开关。
    新装的默认只开 minimax / deepseek / zhipu —— 其余 5 家得自己填 key。
    """
    reg = _registry()
    order = list(prov.ORDER) if prov is not None else []
    enabled_on = set(prov.DEFAULT_ENABLED) if prov is not None else set()
    out = {
        "order": order,
        # 哪个后端参与请求（关掉就完全不碰它，一次请求都不发）
        "enabled": dict((n, (n in enabled_on)) for n in reg),
    }
    for n, p in reg.items():
        out["%s_model" % n] = p.get("default_model", "")
        out["%s_base_url" % n] = p.get("base_url", "")
        out["%s_api_key" % n] = ""
    return out


def _provider_specs():
    """按注册表给每家厂商生成 4 个控件：启用开关 / 密钥 / 模型下拉 / 接口地址。

    加一家新厂商 = 在 providers.py 里加一行，这里自动多出四个控件。
    """
    out = []
    for name, p in _registry().items():
        pair = p.get("label") or (name, name)
        zh = pair[0]
        en = pair[1] if len(pair) > 1 else pair[0]
        out.append({
            "key": "llm.enabled.%s" % name, "tab": "模型",
            "label": "启用 %s" % zh, "en_label": "Enable %s" % en,
            "type": "bool", "provider": name,
            "hint": "关掉就完全不请求这个后端。",
            "en_hint": "When off, this backend is never contacted."})
        out.append({
            "key": "api_key.%s" % name, "tab": "模型",
            "label": "%s 接口密钥" % zh, "en_label": "%s API key" % en,
            "type": "secret", "secret_name": name, "provider": name,
            "hint": "填了会写进密钥库（config/api_keys.json，不进版本库）；留空表示不修改现有 key。",
            "en_hint": "If filled it is written to the key store (config/api_keys.json, never committed). Leave blank to keep the current key."})
        # 说明文字结尾的句号要去掉 —— 后面还要接"；清单是……"，拼起来变成 "…keys.；the list was…" 读着别扭
        note_zh = (p.get("note") or "").rstrip("。. ")
        note_en = (p.get("note_en") or p.get("note") or "").rstrip(". ")
        if p.get("verified"):
            how = "清单是实测出来的（服务端 /models + 逐个真实请求验活）"
            how_en = ("the list was measured (server /models plus a live call per model)")
        else:
            how = "清单抄自官方文档（本机没 key 没法验活），填上 key 后点「拉取最新」换成服务端真实清单"
            how_en = ("the list comes from the official docs (no key here to verify it) - add a key"
                      " and hit Refresh to pull the real list from the server")
        out.append({
            "key": "llm.%s_model" % name, "tab": "模型",
            "label": "%s 模型" % zh, "en_label": "%s model" % en,
            "type": "choice", "provider": name,
            "choices": list(p.get("models") or []),
            "editable": True,
            "hint": "%s；%s。可以自己输入清单里没有的模型名。" % (note_zh, how),
            "en_hint": "%s; %s. You can type a model name that is not in the list."
                       % (note_en, how_en)})
        out.append({
            "key": "llm.%s_base_url" % name, "tab": "模型",
            "label": "%s 地址" % zh, "en_label": "%s URL" % en,
            "type": "text", "provider": name,
            "hint": "接口地址。用自建网关或代理的话改这里。",
            "en_hint": "The endpoint. Change it if you use a self-hosted gateway or proxy."})
    return out


def _provider_items():
    """后端优先级下拉里的原始值（厂商代号）。"""
    return list(prov.ORDER) if prov is not None else list(_registry().keys())


def _provider_items_i18n():
    """后端优先级的本地化显示名：{语言: {代号: 名字}}。"""
    if prov is not None:
        return prov.labels_i18n()
    return {"zh": {}, "en": {}}


def provider_order():
    """厂商顺序 —— 面板分组、后端自检遍历都用它。"""
    return _provider_items()


def provider_label(name, lang="zh"):
    """厂商的本地化显示名（面板上显示的是这个，不是代号）。"""
    if prov is not None:
        return prov.label_of(name, lang)
    return name


# ------------------------------------------------------------------ 默认值
# 这份就是当前 config.json 里那套已验收的参数
DEFAULTS = {
    "hotkey": "ctrl+alt+space",
    "xiaomi_scan_code": 114,
    "xiaomi_vk": 255,
    "swallow_matched_key": True,
    "log_all_keys": True,
    "show_orb": False,
    "orb_size": 200,
    "orb_bottom_margin": 92,
    "caption_max_width": 900,
    "caption_font_size": 14,        # 字幕卡正文字号
    "caption_position": "top",
    "wave_delay_sec": 0.2,
    "wave_sweep_sec": 0.5,
    "wave_band_frac": 0.032,
    "glow_origin": [0.9236, 0.9711],
    "glow_intensity": 1.0,          # 边光亮度上限（倍率，1.0 = 原始亮度）
    "show_tray_icon": True,         # 托盘图标
    "autostart": True,              # 开机自启
    "sim_rate": 4.2,                # 模拟电平包络速率（level_source=sim 时用）
    # ---- 中英双语三层 ----
    "ui_lang": "zh",        # 界面语言：zh | en
    "reply_lang": "zh",     # 回复语言：zh | en | both（both = 中英对照）
    # llm 段**按 providers.py 的注册表自动生成**（模型 / 地址 / 开关 / 优先级）
    "llm": _llm_defaults(),
    # ---- 助理能力（操作权限）----
    # 总开关默认关；鼠标键盘 / 执行命令风险最高，各自再要一道单独的开关。
    # work_dir 为空 = 用「我的文档/Siri-OS」，文件类工具锁死在里面。
    "agent": {
        "enabled": False,
        "work_dir": "",
        "allow_screen": False,
        "allow_shell": False,
        "max_rounds": 6,
    },
    # ---- 上下文 ----
    # 对话记忆只存在**内存里**（关掉程序就没了），不写文件、不进 config.json。
    # 电脑状态是每次请求都附带的一小段摘要（前台窗口 / 窗口清单 / 分辨率 / 工作目录）。
    "ctx": {
        "enabled": True,
        "history_turns": 6,
        "sysinfo": True,
    },
    # stt.language: auto | zh-CN | en-US
    #   auto = 上次成功的语言优先，这次没听清就换另一种再试一次
    # stt.mode: winh = 走 Windows 自带语音输入（Win+H）；offline = 本机 System.Speech
    "stt": {"timeout_sec": 10, "language": "auto", "mode": "winh",
            "hide_panel": True, "mute_start_sound": True, "mute_seconds": 1.8,
            "hide_max_seconds": 120.0},
    "tts": {"voice_hint": "Huihui", "rate": 0},
    # ⚠️ 语言中性：只说「你是谁、怎么说话」，不说用哪种语言 ——
    #    语言规则由 i18n.LANG_RULES 按 reply_lang 追加，用户改这段也不会失效。
    "system_prompt": ("你是运行在用户电脑上的语音助手。用简短自然的口语回答，"
                      "一到两句话，不要用 Markdown、不要列点、不要表情符号。"),
    "marquee_period_sec": 6.0,
    "marquee_trail": 0.09,
    "level_source": "mic",
    "level_gain": 1.0,
}


# 模型清单 = 注册表里的 models（各家一份，见 providers.py）
# 有 key 的 3 家是**实测**出来的（服务端 /models + 逐个真实请求验活）；
# 其余 5 家抄官方文档 —— 填上 key 后点「拉取最新」就能换成服务端真实清单。
MODEL_CHOICES = dict((n, list(p.get("models") or [])) for n, p in _registry().items())


# ------------------------------------------------------------ 旋钮的数据描述
# key 用点号路径；列表下标写成 glow_origin[0]
# type: float / int / bool / text / textlong / choice / order
# en_label / en_hint / en_unit：面板切英文时用（中文是主语言）
# choices_i18n: {"zh": {值: 显示}, "en": {值: 显示}} —— 只影响显示，存进 config 的还是值
SPEC = [
    # ---------------- 扩散
    {"key": "wave_sweep_sec", "tab": "扩散", "label": "扩散时长", "en_label": "Diffusion duration",
     "type": "float", "min": 0.10, "max": 2.00, "step": 0.05, "decimals": 2,
     "unit": "秒", "en_unit": "s",
     "hint": "按下后光带从波源铺到屏幕边缘要多久。参考视频约 0.5 秒。",
     "en_hint": "How long the glow takes to reach the screen edge from its source. The reference video is about 0.5 s."},
    {"key": "wave_delay_sec", "tab": "扩散", "label": "按下延迟", "en_label": "Press delay",
     "type": "float", "min": 0.0, "max": 1.00, "step": 0.05, "decimals": 2,
     "unit": "秒", "en_unit": "s",
     "hint": "按下之后先暗多少秒，光才开始扩散。",
     "en_hint": "How long the screen stays dark after the press before the glow starts spreading."},
    {"key": "wave_band_frac", "tab": "扩散", "label": "光带宽度", "en_label": "Glow band width",
     "type": "float", "min": 0.010, "max": 0.100, "step": 0.002, "decimals": 3,
     "unit": "屏宽比例", "en_unit": "screen width",
     "hint": "扩散光带的厚度。",
     "en_hint": "Thickness of the spreading glow band."},
    {"key": "glow_origin[0]", "tab": "扩散", "label": "波源位置 X", "en_label": "Glow origin X",
     "type": "float", "min": 0.0, "max": 1.0, "step": 0.005, "decimals": 4,
     "unit": "屏幕比例", "en_unit": "screen",
     "hint": "光从哪儿炸开。默认在任务栏右下角「电量与日期之间」那道缝。",
     "en_hint": "Where the glow starts. Defaults to the gap between battery and clock in the taskbar."},
    {"key": "glow_origin[1]", "tab": "扩散", "label": "波源位置 Y", "en_label": "Glow origin Y",
     "type": "float", "min": 0.0, "max": 1.0, "step": 0.005, "decimals": 4,
     "unit": "屏幕比例", "en_unit": "screen",
     "hint": "同上，纵向位置。",
     "en_hint": "Same as above, vertical position."},
    {"key": "glow_intensity", "tab": "扩散", "label": "边光亮度", "en_label": "Glow brightness",
     "type": "float", "min": 0.20, "max": 1.00, "step": 0.05, "decimals": 2,
     "unit": "上限", "en_unit": "cap",
     "hint": "边光的亮度上限。1.0 是原始亮度，调低可以让整体更含蓄。",
     "en_hint": "Upper bound on the glow brightness. 1.0 is the original brightness; lower values make it subtler."},

    # ---------------- 跑马灯 / 画面
    {"key": "marquee_period_sec", "tab": "跑马灯", "label": "跑马灯一圈", "en_label": "Marquee lap",
     "type": "float", "min": 1.0, "max": 30.0, "step": 0.5, "decimals": 1,
     "unit": "秒", "en_unit": "s",
     "hint": "亮头绕屏幕一圈要几秒。全程恒速，状态之间不变速。",
     "en_hint": "Seconds for the bright head to travel once around the screen. Constant speed at all times."},
    {"key": "marquee_trail", "tab": "跑马灯", "label": "尾巴长度", "en_label": "Trail length",
     "type": "float", "min": 0.02, "max": 0.30, "step": 0.005, "decimals": 3,
     "unit": "圈的比例", "en_unit": "of a lap",
     "hint": "亮头后面拖多长。0.09 ≈ 32 度。",
     "en_hint": "How far the trail lags behind the bright head. 0.09 is about 32 degrees."},
    {"key": "caption_position", "tab": "跑马灯", "label": "字幕卡位置", "en_label": "Caption position",
     "type": "choice", "choices": ["top", "bottom"],
     "choices_i18n": {"zh": {"top": "顶部", "bottom": "底部"},
                      "en": {"top": "Top", "bottom": "Bottom"}},
     "hint": "语音对话框浮在屏幕顶部还是底部。",
     "en_hint": "Whether the voice dialog floats at the top or the bottom of the screen."},
    {"key": "caption_max_width", "tab": "跑马灯", "label": "卡片最大宽度", "en_label": "Card max width",
     "type": "int", "min": 400, "max": 1200, "step": 20,
     "unit": "像素", "en_unit": "px",
     "hint": "字幕卡最多占多宽。",
     "en_hint": "How wide the caption card may get."},
    {"key": "caption_font_size", "tab": "跑马灯", "label": "字幕字号", "en_label": "Caption font size",
     "type": "int", "min": 10, "max": 28, "step": 1,
     "unit": "像素", "en_unit": "px",
     "hint": "字幕卡正文的字号。",
     "en_hint": "Font size of the caption card body text."},
    {"key": "orb_size", "tab": "跑马灯", "label": "光球大小", "en_label": "Orb size",
     "type": "int", "min": 80, "max": 420, "step": 10,
     "unit": "像素", "en_unit": "px",
     "hint": "中心光球的直径（只有打开「显示中心光球」才有效）。",
     "en_hint": "Diameter of the centre orb (only used when the centre orb is enabled)."},
    {"key": "orb_bottom_margin", "tab": "跑马灯", "label": "光球离底距离", "en_label": "Orb bottom margin",
     "type": "int", "min": 0, "max": 300, "step": 4,
     "unit": "像素", "en_unit": "px",
     "hint": "中心光球离屏幕底边多远。",
     "en_hint": "How far the centre orb sits above the bottom edge."},
    {"key": "sim_rate", "tab": "跑马灯", "label": "模拟语速", "en_label": "Simulated speech rate",
     "type": "float", "min": 1.0, "max": 12.0, "step": 0.2, "decimals": 1,
     "unit": "音节/秒", "en_unit": "syllables/s",
     "hint": "电平来源选「模拟」时，造出来的音节包络跑多快。",
     "en_hint": "How fast the simulated syllable envelope runs when the level source is simulated."},
    {"key": "show_orb", "tab": "跑马灯", "label": "显示中心光球", "en_label": "Show center orb",
     "type": "bool",
     "hint": "参考视频里没有中间那个光球，默认关。",
     "en_hint": "The reference video has no center orb — off by default."},
    {"key": "level_source", "tab": "跑马灯", "label": "电平来源", "en_label": "Level source",
     "type": "choice", "choices": ["mic", "sim"],
     "choices_i18n": {"zh": {"mic": "麦克风", "sim": "模拟"},
                      "en": {"mic": "Microphone", "sim": "Simulated"}},
     "hint": "mic=真麦克风电平（录声时边光往里钻）；sim=模拟，用来在麦克风坏掉时看效果。",
     "en_hint": "mic = the real microphone level (the glow pulls inward while you speak); sim = simulated, for when the mic is broken."},
    {"key": "level_gain", "tab": "跑马灯", "label": "电平增益", "en_label": "Level gain",
     "type": "float", "min": 0.2, "max": 3.0, "step": 0.1, "decimals": 2,
     "unit": "倍", "en_unit": "x",
     "hint": "录声时边光向内收缩的幅度倍率。",
     "en_hint": "Multiplier for how deep the glow pulls inward while recording."},

    # ---------------- 模型
    {"key": "llm.order", "tab": "模型", "label": "后端优先级", "en_label": "Backend priority",
     "type": "order", "items": _provider_items(), "items_i18n": _provider_items_i18n(),
     "hint": "从上到下依次尝试；鉴权/余额出错的会自动跳过本次会话。",
     "en_hint": "Tried top to bottom; backends with auth or balance errors are skipped for this session."},
    {"key": "system_prompt", "tab": "模型", "label": "系统提示词", "en_label": "System prompt",
     "type": "textlong",
     "hint": "它决定助手的说话风格。语言规则会按「回复语言」另外追加，不用自己写语言要求。",
     "en_hint": "It decides how the assistant talks. Language rules are appended separately per reply language, so you do not need to state a language here."},
    # ---------------- 语音
    {"key": "stt.timeout_sec", "tab": "语音", "label": "听写超时", "en_label": "Recognition timeout",
     "type": "int", "min": 5, "max": 30, "step": 1,
     "unit": "秒", "en_unit": "s",
     "hint": "按一次键，最多听多久。听不到就问「没听清，再说一次？」",
     "en_hint": "How long one key press listens. If nothing is heard it asks to say that again."},
    {"key": "stt.mode", "tab": "语音", "label": "听写方式", "en_label": "Dictation input",
     "type": "choice", "choices": ["winh", "offline"],
     "choices_i18n": {"zh": {"winh": "Win+H 语音输入", "offline": "本机离线听写"},
                      "en": {"winh": "Win+H voice typing", "offline": "Offline dictation"}},
     "hint": "Win+H 用 Windows 自带的语音输入；本机离线听写走 System.Speech，不联网。",
     "en_hint": "Win+H uses the built-in Windows voice typing; offline dictation uses System.Speech locally."},
    {"key": "stt.hide_panel", "tab": "语音", "label": "隐藏语音输入面板", "en_label": "Hide voice typing panel",
     "type": "bool",
     "hint": "Win+H 会弹出 Windows 自己的语音输入条/提示卡，这里把它自动藏起来（只藏这一小会儿，不影响听写本身，也不碰中文输入法候选窗）。",
     "en_hint": "Win+H shows Windows' own voice typing bar/toast. This hides it automatically; dictation itself is unaffected."},
    {"key": "stt.mute_start_sound", "tab": "语音", "label": "静音开始录音提示音", "en_label": "Mute start-of-recording sound",
     "type": "bool",
     "hint": "刚开始录音那声「叮」，按一次键的工夫内把语音输入自己那个音频会话的音量压掉再恢复（只压这一小会儿）。",
     "en_hint": "Silences the beep when recording starts by briefly muting the voice typing audio session, then restoring it."},
    {"key": "tts.voice_hint", "tab": "语音", "label": "播报音色", "en_label": "Voice",
     "type": "choice", "choices": ["Huihui", "Kangkang", "Yunyang", "Xiaoxiao", "Zira", "David"],
     "editable": True,
     "hint": "本机装了中文声线 Huihui/Kangkang 和英文声线 Zira/David，英文回复会自动切英文声线。",
     "en_hint": "Chinese voices Huihui/Kangkang and English voices Zira/David are installed; English replies switch to an English voice automatically."},
    {"key": "tts.rate", "tab": "语音", "label": "语速", "en_label": "Speech rate",
     "type": "int", "min": -10, "max": 10, "step": 1,
     "hint": "负数更慢，正数更快。",
     "en_hint": "Negative is slower, positive is faster."},

    # ---------------- 键位
    {"key": "hotkey", "tab": "键位", "label": "兜底热键", "en_label": "Fallback hotkey",
     "type": "text",
     "hint": "小米键之外的备用唤醒方式，格式如 ctrl+alt+space。",
     "en_hint": "A backup way to wake the assistant besides the Xiaomi key, e.g. ctrl+alt+space."},
    {"key": "xiaomi_scan_code", "tab": "键位", "label": "小米键扫描码", "en_label": "Xiaomi key scan code",
     "type": "int", "min": 0, "max": 255, "step": 1,
     "hint": "0 = 不绑定。当前这台机器是 114 (0x72)。",
     "en_hint": "0 means unbound. On this machine it is 114 (0x72)."},
    {"key": "xiaomi_vk", "tab": "键位", "label": "小米键 VK", "en_label": "Xiaomi key VK",
     "type": "int", "min": 0, "max": 255, "step": 1,
     "hint": "0 = 不绑定。当前是 255。",
     "en_hint": "0 means unbound. Currently 255."},
    {"key": "swallow_matched_key", "tab": "键位", "label": "吞掉这个按键", "en_label": "Swallow the key",
     "type": "bool",
     "hint": "按小米键时不让别的程序收到。",
     "en_hint": "Other apps do not see the Xiaomi key press."},
    {"key": "log_all_keys", "tab": "键位", "label": "记录所有按键", "en_label": "Log all keys",
     "type": "bool",
     "hint": "调试用，会把每一次按键写进 keyhook.log。",
     "en_hint": "For debugging: every key press is written to keyhook.log."},

    # ---------------- 语言（中英双语三层）
    {"key": "ui_lang", "tab": "语言", "label": "界面语言", "en_label": "Interface language",
     "type": "choice", "choices": ["zh", "en"],
     "choices_i18n": {"zh": {"zh": "中文", "en": "英文"},
                      "en": {"zh": "Chinese", "en": "English"}},
     "hint": "设置面板和屏幕上的状态文字（聆听中/思考中/说话中）用哪种语言。",
     "en_hint": "Which language the settings panel and the on-screen status label use."},
    {"key": "reply_lang", "tab": "语言", "label": "回复语言", "en_label": "Reply language",
     "type": "choice", "choices": ["zh", "en", "both"],
     "choices_i18n": {"zh": {"zh": "中文", "en": "英文", "both": "中英对照"},
                      "en": {"zh": "Chinese", "en": "English", "both": "Both (side by side)"}},
     "hint": "助手回答用哪种语言。「中英对照」= 先中文再英文，两段都显示；播报时只念当前声线那种语言。",
     "en_hint": "Which language the assistant answers in. \"Both\" means Chinese then English, both shown; only one language is spoken aloud."},
    {"key": "stt.language", "tab": "语言", "label": "听写语言", "en_label": "Recognition language",
     "type": "choice", "choices": ["auto", "zh-CN", "en-US"],
     "choices_i18n": {"zh": {"auto": "自动", "zh-CN": "中文", "en-US": "英文"},
                      "en": {"auto": "Auto", "zh-CN": "Chinese", "en-US": "English"}},
     "hint": "自动 = 上次成功的语言优先，这次没听清就换另一种再试一次（麦克风崩溃时不重试）。",
     "en_hint": "Auto prefers the language that worked last and retries with the other one if nothing is heard (no retry when the mic crashes)."},

    # ---------------- 助理（操作权限）
    {"key": "agent.enabled", "tab": "助理", "label": "启用助理能力",
     "en_label": "Enable assistant abilities",
     "type": "bool",
     "hint": "关掉的话助手只会聊天，不会碰你的文件、网页、图片和电脑。",
     "en_hint": "When off the assistant only chats - it never touches your files, web pages, images or the computer."},
    {"key": "agent.work_dir", "tab": "助理", "label": "工作目录",
     "en_label": "Working folder",
     "type": "text",
     "hint": "助手能读写的文件范围。留空 = 我的文档/Siri-OS。越出这个目录的文件一律拒绝。",
     "en_hint": "The only folder the assistant may read and write. Empty means Documents/Siri-OS; anything outside is refused."},
    {"key": "agent.allow_screen", "tab": "助理", "label": "允许操控电脑",
     "en_label": "Allow computer control",
     "type": "bool",
     "hint": "鼠标点击、键盘输入、截图、切窗口、开程序。风险最高的一档，默认关。",
     "en_hint": "Mouse clicks, typing, screenshots, switching windows, launching apps. The riskiest group, off by default."},
    {"key": "agent.allow_shell", "tab": "助理", "label": "允许执行命令",
     "en_label": "Allow running commands",
     "type": "bool",
     "hint": "在工作目录里跑命令行。默认关 —— 打开前想清楚要给它什么权限。",
     "en_hint": "Run command lines inside the working folder. Off by default - think about what you are granting."},
    {"key": "agent.max_rounds", "tab": "助理", "label": "单轮最多调用工具",
     "en_label": "Max tool rounds",
     "type": "int", "min": 1, "max": 12, "step": 1,
     "unit": "次", "en_unit": "rounds",
     "hint": "一次提问里最多连续调几次工具，防止绕圈。",
     "en_hint": "How many tool calls may be chained for one question, so it cannot loop forever."},

    # ---------------- 上下文
    {"key": "ctx.enabled", "tab": "上下文", "label": "记住前几轮对话",
     "en_label": "Remember previous turns",
     "type": "bool",
     "hint": "关掉的话每句都是新话题，「把它改短一点」这种接不上。只存在内存里，不落盘。",
     "en_hint": "When off every question starts fresh, so 'make it shorter' has nothing to refer to. Kept in memory only, never written to disk."},
    {"key": "ctx.history_turns", "tab": "上下文", "label": "记住几轮",
     "en_label": "Turns to remember",
     "type": "int", "min": 0, "max": 20, "step": 1,
     "unit": "轮", "en_unit": "turns",
     "hint": "一轮 = 你说一句 + 助手答一句。0 = 完全不记。",
     "en_hint": "One turn = you speak and the assistant answers. 0 means no memory at all."},
    {"key": "ctx.sysinfo", "tab": "上下文", "label": "附带电脑状态",
     "en_label": "Include computer state",
     "type": "bool",
     "hint": "每次都告诉模型：当前开着哪些窗口、屏幕多大、工作目录在哪。要看屏幕画面，模型可以自己截图。",
     "en_hint": "Tell the model every turn which windows are open, the screen size and the working folder. To see the actual screen it can take a screenshot itself."},

    # ---------------- 系统
    {"key": "autostart", "tab": "系统", "label": "开机自启", "en_label": "Start at login",
     "type": "bool",
     "hint": "在「启动」文件夹放一个快捷方式（当前用户级，不需要管理员权限）。改完点保存生效。",
     "en_hint": "Drops a shortcut into the Startup folder (per user, no admin rights needed). Takes effect when you save."},
    {"key": "show_tray_icon", "tab": "系统", "label": "显示托盘图标", "en_label": "Show tray icon",
     "type": "bool",
     "hint": "关掉就没有托盘图标了 —— 也就打不开设置、没法从托盘退出（停不掉的话用 install_autostart.py stop）。",
     "en_hint": "With it off there is no tray icon, so you cannot open settings or quit from the tray (use install_autostart.py stop to stop it)."},
] + _provider_specs()

TABS = ["扩散", "跑马灯", "模型", "助理", "上下文", "语音", "键位", "系统", "语言"]
TAB_EN = {
    "扩散": "Diffusion",
    "跑马灯": "Marquee",
    "模型": "Model",
    "语音": "Voice",
    "键位": "Keys",
    "系统": "System",
    "语言": "Language",
    "助理": "Assistant",
    "上下文": "Context",
}


# -------------------------------------------------------------- 路径读写
def split_path(path):
    """'llm.minimax_model' -> ['llm','minimax_model']；'glow_origin[0]' -> ['glow_origin', 0]"""
    out = []
    for part in path.split("."):
        if part.endswith("]") and "[" in part:
            name, idx = part[:-1].split("[", 1)
            out.append(name)
            out.append(int(idx))
        else:
            out.append(part)
    return out


def get_path(cfg, path):
    cur = cfg
    for p in split_path(path):
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list) and isinstance(p, int):
            cur = cur[p] if p < len(cur) else None
        else:
            return None
    return cur


def set_path(cfg, path, value):
    parts = split_path(path)
    cur = cfg
    for p in parts[:-1]:
        if isinstance(cur, dict):
            cur = cur.setdefault(p, {})
        elif isinstance(cur, list):
            cur = cur[p]
    last = parts[-1]
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list):
        cur[last] = value
    return cfg


def merge(base, new):
    """深合并：dict 递归，其它直接覆盖。"""
    out = dict(base)
    for k, v in (new or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    """读配置；坏文件不炸 —— 缺的值用 DEFAULTS 补齐。

    配置文件不存在时**自动落一份默认配置**：
    别人 clone 下来直接跑就行，不用先手动 copy config.example.json。
    """
    path = path or CONFIG_PATH
    cfg = merge(DEFAULTS, {})
    if path == CONFIG_PATH and not os.path.exists(path):
        try:
            save(cfg, path)
        except Exception:
            pass
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg = merge(cfg, data)
    except Exception:
        pass
    return cfg


def save(cfg, path=None):
    """原子写：先写临时文件再 os.replace —— 常驻助手不会读到半个 JSON。"""
    path = path or CONFIG_PATH
    d = os.path.dirname(path) or "."
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg_", suffix=".tmp")
    try:
        with io.open(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
            f.write("\n")
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return True


def resident_pid():
    """常驻助手自己的 pid（写在 logs/siri_orb.pid）；没在跑返回 None。"""
    try:
        with io.open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        return None
    if not pid:
        return None
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return pid
    except Exception:
        pass
    return None
