# -*- coding: utf-8 -*-
"""连通性自检：MiniMax + DeepSeek + 智谱 GLM（key 从统一密钥库 config/api_keys.json 读）。"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from llm import MinimaxBackend, DeepSeekBackend, ZhipuBackend


def main():
    cfg = {}
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass
    l = cfg.get("llm") or {}

    bs = [
        ("minimax", MinimaxBackend(model=l.get("minimax_model"),
                                   api_key=l.get("minimax_api_key"),
                                   base_url=l.get("minimax_base_url"))),
        ("deepseek", DeepSeekBackend(model=l.get("deepseek_model"),
                                     api_key=l.get("deepseek_api_key"),
                                     base_url=l.get("deepseek_base_url"))),
        ("zhipu", ZhipuBackend(model=l.get("zhipu_model"),
                               api_key=l.get("zhipu_api_key"),
                               base_url=l.get("zhipu_base_url"))),
    ]
    ok = 0
    for name, b in bs:
        print("%s: base=%s model=%s key=%s" % (
            name, b.base_url, b.model, "yes(%d)" % len(b.api_key) if b.available() else "NO"))
        if not b.available():
            continue
        try:
            out = b.chat([{"role": "user", "content": "用一句中文说你好"}], max_tokens=120)
            print("  %s OK -> %s" % (name, out.replace("\n", " ")[:200]))
            ok += 1
        except Exception as e:
            print("  %s FAIL -> %s" % (name, e))
    print("")
    print("%d/%d live backends OK" % (ok, len(bs)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
