# -*- coding: utf-8 -*-
"""中文听写准确率实测：录 N 秒，把识别文本原样打出来。

用法：
    python tools\siri_orb\mic_test.py          # 10 秒
    python tools\siri_orb\mic_test.py 20       # 20 秒
输出同时写到 tools\siri_orb\logs\mic_test.txt（UTF-8，避免控制台 GBK 乱码）。
"""
import io
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN = os.path.join(HERE, "listen.ps1")
OUT = os.path.join(HERE, "logs", "mic_test.txt")


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    tmp = os.path.join(HERE, "tmp", "mic_test_%d.txt" % int(time.time()))
    try:
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
    except Exception:
        pass
    print("RECORDING %d s -- speak Chinese now ..." % secs, flush=True)
    t0 = time.time()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", LISTEN,
           "-OutFile", tmp, "-TimeoutSec", str(secs), "-Language", "zh-CN"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=secs + 20,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        status = r.stdout.decode("utf-8", "ignore").strip()
    except Exception as e:
        print("listen.ps1 failed: %s" % e)
        return 1
    print("engine: %s (%.1fs)" % (status or "(no output)", time.time() - t0))
    text = ""
    try:
        with io.open(tmp, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with io.open(OUT, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    n = len(text)
    print("recognized chars: %d" % n)
    if text:
        print("transcript (also in logs\\mic_test.txt):")
        print(text)
    else:
        print("NOTHING recognized. Engine opened but heard no usable speech.")
        print("If you did speak, offline SAPI dictation accuracy is too low")
        print("-> switch to the Win+H path instead.")
    return 0 if text else 2


if __name__ == "__main__":
    sys.exit(main())
