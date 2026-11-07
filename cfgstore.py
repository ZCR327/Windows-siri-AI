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

HERE = os.path.dirname(os.path.abspath(__file__))
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
    "caption_position": "top",
    "wave_delay_sec": 0.2,
    "wave_sweep_sec": 0.5,
    "wave_band_frac": 0.032,
    "glow_origin": [0.9236, 0.9711],
    # ---- 中英双语三层 ----
    "ui_lang": "zh",        # 界面语言：zh | en
    "reply_lang": "zh",     # 回复语言：zh | en | both（both = 中英对照）
    "llm": {
        "order": ["minimax", "deepseek", "zhipu"],
        "minimax_model": "MiniMax-M3.1-Flash",
        "minimax_api_key": "",
        "minimax_base_url": "https://api.minimaxi.com/v1",
        "deepseek_model": "deepseek-chat",
        "deepseek_api_key": "",
        "deepseek_base_url": "https://api.deepseek.com",
        "zhipu_model": "glm-4-flash",
        "zhipu_api_key": "",
        "zhipu_base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    # stt.language: auto | zh-CN | en-US
    #   auto = 上次成功的语言优先，这次没听清就换另一种再试一次
    "stt": {"timeout_sec": 10, "language": "auto"},
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
     "type": "order", "items": ["minimax", "deepseek", "zhipu"],
     "hint": "从上到下依次尝试；鉴权/余额出错的会自动跳过本次会话。",
     "en_hint": "Tried top to bottom; backends with auth or balance errors are skipped for this session."},
    {"key": "llm.minimax_model", "tab": "模型", "label": "MiniMax 模型", "en_label": "MiniMax model",
     "type": "choice",
     "choices": ["MiniMax-M3.1-Flash", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-Text-01"],
     "editable": True,
     "hint": "国内站 api.minimaxi.com。可以自己输入库里没有的模型名。",
     "en_hint": "China site api.minimaxi.com. You can type a model name that is not in the list."},
    {"key": "llm.deepseek_model", "tab": "模型", "label": "DeepSeek 模型", "en_label": "DeepSeek model",
     "type": "choice", "choices": ["deepseek-chat", "deepseek-reasoner"], "editable": True,
     "hint": "DeepSeek 的模型名。",
     "en_hint": "The DeepSeek model name."},
    {"key": "llm.zhipu_model", "tab": "模型", "label": "智谱 GLM 模型", "en_label": "Zhipu GLM model",
     "type": "choice", "choices": ["glm-4-flash", "glm-4-plus", "glm-4"], "editable": True,
     "hint": "智谱的模型名。",
     "en_hint": "The Zhipu model name."},
    {"key": "system_prompt", "tab": "模型", "label": "系统提示词", "en_label": "System prompt",
     "type": "textlong",
     "hint": "它决定助手的说话风格。语言规则会按「回复语言」另外追加，不用自己写语言要求。",
     "en_hint": "It decides how the assistant talks. Language rules are appended separately per reply language, so you do not need to state a language here."},
    {"key": "llm.minimax_base_url", "tab": "模型", "label": "MiniMax 地址", "en_label": "MiniMax URL",
     "type": "text",
     "hint": "中国站是 api.minimaxi.com；国际站是 api.minimax.io（key 不能混用）。",
     "en_hint": "China site api.minimaxi.com; international api.minimax.io. Keys are not interchangeable."},
    {"key": "llm.deepseek_base_url", "tab": "模型", "label": "DeepSeek 地址", "en_label": "DeepSeek URL",
     "type": "text", "hint": "DeepSeek 的接口地址。", "en_hint": "The DeepSeek endpoint."},
    {"key": "llm.zhipu_base_url", "tab": "模型", "label": "智谱地址", "en_label": "Zhipu URL",
     "type": "text", "hint": "智谱的接口地址。", "en_hint": "The Zhipu endpoint."},

    # ---------------- 语音
    {"key": "stt.timeout_sec", "tab": "语音", "label": "听写超时", "en_label": "Recognition timeout",
     "type": "int", "min": 5, "max": 30, "step": 1,
     "unit": "秒", "en_unit": "s",
     "hint": "按一次键，最多听多久。听不到就问「没听清，再说一次？」",
     "en_hint": "How long one key press listens. If nothing is heard it asks to say that again."},
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
]

TABS = ["扩散", "跑马灯", "模型", "语音", "键位", "语言"]
TAB_EN = {
    "扩散": "Diffusion",
    "跑马灯": "Marquee",
    "模型": "Model",
    "语音": "Voice",
    "键位": "Keys",
    "语言": "Language",
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
    """读配置；坏文件不炸 —— 缺的值用 DEFAULTS 补齐。"""
    path = path or CONFIG_PATH
    cfg = merge(DEFAULTS, {})
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
