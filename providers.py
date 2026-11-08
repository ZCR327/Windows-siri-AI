# -*- coding: utf-8 -*-
"""后端注册表：一家厂商一行 —— 协议 / 地址 / 模型清单全在这。

为什么单独一个文件（**零 import**）：
  - `cfgstore.py`（配置层，生成面板控件）和 `llm.py`（协议层，发请求）
    都要读这张表。放在任何一边都会造成循环依赖，或者把 requests 拖进配置层；
  - 零依赖 = 谁都能 import —— 打包成 exe 时也不会多拉一堆东西。

字段：
  label        (中文名, 英文名) —— 面板上显示的名字
  protocol     anthropic(/messages) | openai(/chat/completions)
  base_url     接口地址
  models_url   模型清单地址（「拉取最新」按钮打它）
  env          环境变量名（没配 key 时的兜底）
  default_model 默认模型
  models       模型清单
  verified     True = 清单是**实测**出来的；False = 抄官方文档（本机没 key，验不了）
  note         面板提示里的补充说明
"""

PROVIDERS = {
    "minimax": {
        "label": ("MiniMax", "MiniMax"),
        "protocol": "anthropic",
        "base_url": "https://api.minimaxi.com/anthropic/v1",
        "models_url": "https://api.minimaxi.com/v1/models",
        "env": "MINIMAX_API_KEY",
        "default_model": "MiniMax-M3.1-Flash",
        "verified": True,
        "note": "国内站 api.minimaxi.com 的 Anthropic 兼容端点；国际站 api.minimax.io（key 不能混用）。",
        "note_en": "China site api.minimaxi.com (Anthropic-compatible endpoint); the international site api.minimax.io does not take the same keys.",
        "models": [
            "MiniMax-M3.1-Flash", "MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M2.1", "MiniMax-M2.1-highspeed",
            "MiniMax-M2",
        ],
    },
    "deepseek": {
        "label": ("DeepSeek", "DeepSeek"),
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "models_url": "https://api.deepseek.com/models",
        "env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "verified": True,
        "note": "官方在售 v4-pro / flash；chat / reasoner / coder 是还能调通的旧别名。",
        "note_en": "v4-pro and flash are the current models; chat, reasoner and coder are older aliases that still work.",
        "models": [
            "deepseek-v4-pro", "deepseek-flash", "deepseek-chat",
            "deepseek-reasoner", "deepseek-coder",
        ],
    },
    "zhipu": {
        "label": ("智谱 GLM", "Zhipu GLM"),
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models_url": "https://open.bigmodel.cn/api/paas/v4/models",
        "env": "ZHIPU_API_KEY",
        "default_model": "glm-4-flash",
        "verified": True,
        "note": "5.x 是新一代，4.x 系列仍可用。",
        "note_en": "5.x is the new generation; the 4.x series still works.",
        "models": [
            "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.3-flashx-legal",
            "glm-5.2", "glm-5.1", "glm-5-turbo", "glm-5",
            "glm-4.7", "glm-4.6", "glm-4.5", "glm-4.5-air", "glm-4-flash",
            "glm-4-plus", "glm-4-air", "glm-4v-plus",
        ],
    },
    "claude": {
        "label": ("Claude", "Claude"),
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models_url": "https://api.anthropic.com/v1/models",
        "env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-5-5",
        "verified": False,
        "note": "anthropic 协议（/messages），跟 MiniMax 同一家实现。",
        "note_en": "Anthropic protocol (/messages) - the same implementation MiniMax uses.",
        "models": [
            "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
            "claude-opus-5", "claude-sonnet-5",
            "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
            "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5",
            "claude-opus-4-1", "claude-opus-4", "claude-sonnet-4",
            "claude-sonnet-3-7", "claude-sonnet-3-5", "claude-haiku-3-5",
            "claude-opus-3", "claude-haiku-3",
        ],
    },
    "qwen": {
        "label": ("千问", "Qwen"),
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "env": "DASHSCOPE_API_KEY",
        "default_model": "qwen3.8-max",
        "verified": False,
        "note": "阿里云百炼的 OpenAI 兼容端点。",
        "note_en": "Alibaba Cloud Model Studio's OpenAI-compatible endpoint.",
        "models": [
            "qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus", "qwen3.6-plus",
            "qwen3.5-plus", "qwen3-coder-plus", "qwen3-coder-next",
            "qwen3-max", "qwen3-max-2026-01-23",
            "qwen-plus", "qwen-turbo", "qwen-long", "qwen-max",
        ],
    },
    "kimi": {
        "label": ("Kimi", "Kimi"),
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models_url": "https://api.moonshot.cn/v1/models",
        "env": "MOONSHOT_API_KEY",
        "default_model": "kimi-k3.1-pro",
        "verified": False,
        "note": "月之暗面 Moonshot 的 OpenAI 兼容端点。",
        "note_en": "Moonshot's OpenAI-compatible endpoint.",
        "models": [
            "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3",
            "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6", "kimi-k2.5",
            "kimi-k2-turbo-preview", "kimi-latest",
            "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k",
        ],
    },
    "gemini": {
        "label": ("Gemini", "Gemini"),
        "protocol": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "env": "GEMINI_API_KEY",
        "models_auth": "x-goog-api-key",
        "default_model": "gemini-3.8-flash",
        "verified": False,
        "note": "Google 的 OpenAI 兼容端点（也能用原生 Gemini 协议）。",
        "note_en": "Google's OpenAI-compatible endpoint (the native Gemini protocol works too).",
        "models": [
            "gemini-3.8-flash", "gemini-3.8-live", "gemini-3.6-flash",
            "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
            "gemini-3.1-pro-preview", "gemini-3-pro-preview", "gemini-3-flash-preview",
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
            "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-flash-latest",
            "gemini-omni-flash",
        ],
    },
    "doubao": {
        "label": ("豆包", "Doubao"),
        "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models_url": "https://ark.cn-beijing.volces.com/api/v3/models",
        "env": "ARK_API_KEY",
        "default_model": "doubao-seed-2-1-pro",
        "verified": False,
        "note": "火山引擎方舟的 OpenAI 兼容端点。",
        "note_en": "Volcano Engine Ark's OpenAI-compatible endpoint.",
        "models": [
            "doubao-seed-2-1-pro", "doubao-seed-2-1-lite", "doubao-seed-2-1-turbo",
            "doubao-seed-2-0-pro", "doubao-seed-2-0-lite", "doubao-seed-2-0-mini",
            "doubao-seed-2-0-code", "doubao-seed-2-0-code-preview",
            "doubao-seed-1.6-vision", "doubao-seed-character", "doubao-seed-evolving",
        ],
    },
    "openai": {
        "label": ("ChatGPT", "ChatGPT"),
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "models_url": "https://api.openai.com/v1/models",
        "env": "OPENAI_API_KEY",
        "default_model": "gpt-6.1-sol",
        "verified": False,
        "note": "OpenAI 官方端点。旗舰是 gpt-6-astra，默认用 gpt-6.1-sol（官方定位：性能与成本均衡）。",
        "note_en": "OpenAI's own endpoint. gpt-6-astra is the flagship; gpt-6.1-sol balances intelligence and cost.",
        "models": [
            "gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra", "gpt-6-luna",
            "gpt-6-sol", "gpt-6-cyber",
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-cyber",
            "gpt-5.5", "gpt-5.5-pro",
            "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-pro",
            "gpt-5.2", "gpt-5.2-pro",
            "chat-latest", "gpt-audio-1.5",
        ],
        # 图片模型走另一套端点（/images/generations），不混进对话模型下拉
        "image_models": [
            "gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst",
            "gpt-image-2.6-flare", "gpt-image-2.6-sunburst",
        ],
    },
}

# 面板「后端优先级」里的默认顺序（也是没配置时的尝试顺序）
ORDER = ["minimax", "deepseek", "zhipu", "openai", "claude", "qwen", "kimi", "gemini", "doubao"]

# 新装的用户默认只开这 3 家 —— 其余 5 家得自己填 key 才开
DEFAULT_ENABLED = ["minimax", "deepseek", "zhipu"]


def label_of(name, lang="zh"):
    """厂商显示名（面板上用）。"""
    p = PROVIDERS.get(name) or {}
    pair = p.get("label") or (name, name)
    return pair[0] if lang == "zh" else (pair[1] if len(pair) > 1 else pair[0])


def labels_i18n():
    """给面板的「后端优先级」用：{值: 显示名}，中英各一份。"""
    zh = {}
    en = {}
    for name, p in PROVIDERS.items():
        zh[name] = p["label"][0]
        en[name] = p["label"][1] if len(p["label"]) > 1 else p["label"][0]
    return {"zh": zh, "en": en}