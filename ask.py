# -*- coding: utf-8 -*-
"""不用麦克风，直接打字问 Siri-OS —— 走的是和语音完全一样的链路。

为什么要有它：麦克风那条链路还卡在驱动层（刷机后 Intel 智音采集崩了），
但模型、TTS、本地指令这几段已经通了 —— 这个脚本让你现在就能用上它们，
顺便也是「LLM → TTS」整条链的端到端验证。

用法：
    python tools\siri_orb\ask.py "今天几号"
    python tools\siri_orb\ask.py "用一句话介绍你自己" --speak
    python tools\siri_orb\ask.py "What's the date?" --lang=en
"""
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import i18n
import voice as voice_mod
from llm import LLM


def main():
    raw = sys.argv[1:]
    speak = "--speak" in raw
    lang_override = None
    args = []
    for a in raw:
        if a == "--speak":
            continue
        if a.startswith("--lang="):
            lang_override = a.split("=", 1)[1].strip().lower()
            continue
        args.append(a)
    if not args:
        print('usage: python tools\siri_orb\ask.py "your question" [--speak] [--lang=zh|en|both]')
        return 1
    text = " ".join(args).strip()
    cfg = {}
    try:
        with io.open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    # 补齐缺省（config.json 里可能缺新加的键）
    try:
        import cfgstore
        cfg = cfgstore.merge(cfgstore.DEFAULTS, cfg)
    except Exception:
        pass

    rl = lang_override or i18n.reply_lang(cfg)
    if rl not in i18n.REPLY_LANGS:
        rl = "zh"
    # ⚠ --lang 覆盖必须同时作用到**提示词**，否则模型仍按 config.json 的语言回答
    #   （踩过：--lang=en 提问，模型回的中文）
    cfg = dict(cfg)
    cfg["reply_lang"] = rl

    reply = None
    try:
        reply = voice_mod.try_local_command(text, lang=rl)
    except Exception:
        reply = None
    local = reply is not None

    if not reply:
        llm = LLM(cfg)
        t0 = time.time()
        reply = llm.chat([{"role": "user", "content": text}],
                         system=i18n.build_system_prompt(cfg))
        dt = time.time() - t0
        print("[llm %.2fs]" % dt)

    out = io.open(os.path.join(HERE, "tmp", "ask_last.txt"), "w", encoding="utf-8")
    out.write(reply)
    out.close()
    print(reply)
    print("(source: %s, reply_lang=%s)" % ("local command" if local else "llm", rl))

    if speak:
        sp = voice_mod.Speaker(voice_hint=(cfg.get("tts") or {}).get("voice_hint", "Huihui"),
                               rate=(cfg.get("tts") or {}).get("rate", 0))
        sp.speak_localized(reply, lang=rl)
        print("(spoken)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
