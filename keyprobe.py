# -*- coding: utf-8 -*-
"""扫描码嗅探：找出小米笔记本上那颗闲置「小米语音助手」键的扫描码。

用法：
    python keyprobe.py                 一直监听，按键即打印（Ctrl+C 退出）
    python keyprobe.py --seconds 60    监听 60 秒后自动退出

看输出里哪个 vk / sc 不是你按的普通键，那就是它；把它的 sc 填进 config.json
的 xiaomi_scan_code 即可绑定。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from keyhook import KeyHook, key_name

LOG_PATH = os.path.join(HERE, "logs", "keyprobe.log")


class Probe(KeyHook):
    def _log(self, vk, sc, flags):
        line = "%s vk=0x%02X(%d) sc=0x%02X(%d) flags=0x%02X %s" % (
            time.strftime("%H:%M:%S"), vk, vk, sc, sc, flags, key_name(vk, sc, flags))
        try:
            print(line)
            sys.stdout.flush()
        except Exception:
            pass
        KeyHook._log(self, vk, sc, flags)


def main():
    seconds = 0
    if "--seconds" in sys.argv:
        i = sys.argv.index("--seconds")
        try:
            seconds = int(sys.argv[i + 1])
        except Exception:
            seconds = 60
    p = Probe(hotkey="", log_path=LOG_PATH, log_all=True, swallow=False)
    p.start()
    time.sleep(0.5)
    if p.error:
        print("hook error: %s" % p.error)
        return
    print("listening... press the Xiaomi voice key (a few times). log: %s" % LOG_PATH)
    sys.stdout.flush()
    if seconds:
        time.sleep(seconds)
        print("done")
    else:
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
