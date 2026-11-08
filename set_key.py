# -*- coding: utf-8 -*-
"""把模型 API key 写进**统一密钥库** config/api_keys.json，并立刻发一次真实请求验活。

为什么要有这个脚本：
  key 是密钥，不要贴在聊天里 —— 让自己在这条命令行里粘，
  脚本负责写盘 + 验活，粘完历史留在自己的终端里。
  统一密钥库是为了避免「改了一处、另一处还是旧 key」。

用法：
    python tools\siri_orb\set_key.py            # 交互
    python tools\siri_orb\set_key.py --deepseek # 直接选 deepseek
    python tools\siri_orb\set_key.py --minimax
    python tools\siri_orb\set_key.py --zhipu
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import keys as keylib

CFG = os.path.join(HERE, "config.json")


def _backends():
    """有哪些后端可以设 key —— 按注册表来，别写死。"""
    try:
        import providers as prov
        return tuple(prov.ORDER)
    except Exception:
        return ("minimax", "deepseek", "zhipu", "openai")


BACKENDS = _backends()


def load():
    try:
        with io.open(CFG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(cfg):
    with io.open(CFG, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        f.write("\n")


def ask(prompt, secret=False):
    # ⚠️ Windows 的 getpass 是从 CONIN$ 逐键读的（msvcrt.getwch），
    # 一旦 stdin 被重定向（管道 / 计划任务 / CI）就会**永久阻塞**。
    # 所以只有真控制台才用 getpass，否则退回普通 input()。
    if secret:
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                import getpass
                return getpass.getpass(prompt)
        except Exception:
            pass
    try:
        return input(prompt)
    except EOFError:
        return ""


def clean_key(raw):
    """收拾粘进来的 key；不像 key 就返回 None —— 宁可不写，也别写垃圾进库。"""
    k = (raw or "").strip().strip('"').strip("'")
    k = k.replace("\ufeff", "")            # BOM
    if not k:
        return ""
    if any(ord(c) > 127 for c in k):
        return None                          # 非 ASCII：多半是粘串了/编码乱了
    if len(k) < 16:
        return None                          # 太短：占位符或截断
    return k


def pick():
    arg = ""
    for a in sys.argv[1:]:
        if a.startswith("--") and a[2:] in BACKENDS:
            arg = a[2:]
    if arg:
        return arg
    print("Which backend do you want to set?")
    for i, name in enumerate(BACKENDS, 1):
        print("  %d) %s" % (i, name))
    print("  %d) all" % (len(BACKENDS) + 1))
    ans = ask("choice [1-%d]: " % (len(BACKENDS) + 1)).strip()
    try:
        n = int(ans)
    except ValueError:
        n = 1
    if n == len(BACKENDS) + 1:
        return "all"
    if 1 <= n <= len(BACKENDS):
        return BACKENDS[n - 1]
    return BACKENDS[0]


def make(name, lcfg):
    """按注册表造后端 —— 和助手/面板自检走同一条路（make_backend）。"""
    from llm import make_backend
    return make_backend(name, lcfg)


def test(cfg, name):
    lcfg = cfg.get("llm") or {}
    b = make(name, lcfg)
    if not b.api_key:
        print("  %s: no key set" % name)
        return False
    try:
        # 注意：后端 chat() 没有 timeout 关键字（超时是构造函数参数），传了会 TypeError
        out = b.chat([{"role": "user", "content": "用一句简短中文说你好"}],
                     max_tokens=64)
        print("  %s: LIVE OK -> %s" % (name, out.replace("\n", " ")[:60]))
        return True
    except Exception as e:
        print("  %s: LIVE FAILED -> %s" % (name, e))
        return False


def main():
    cfg = load()
    which = pick()
    names = list(BACKENDS) if which == "all" else [which]
    changed = []
    for name in names:
        print("")
        print("--- %s ---" % name)
        cur = keylib.get(name)
        print("current key length: %d" % len(cur))
        raw = ask("paste %s api key (empty = keep current): " % name, secret=True)
        key = clean_key(raw)
        if key is None:
            print("  [%s] input does not look like a key (non-ascii or <16 chars) -> NOT saved." % name)
            print("  Copy it from the provider console; keys are plain ASCII.")
        elif key:
            keylib.set_key(name, key)
            changed.append(name)
    print("")
    print("key store saved: %s" % keylib.STORE)
    if changed:
        print("updated: %s" % ", ".join(changed))
    ok = True
    for name in names:
        ok = test(cfg, name) and ok
    print("")
    print("RESULT: %s" % ("all live calls OK" if ok else "some checks failed"))
    print("Next: python tools\\siri_orb\\doctor.py --no-mic")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
