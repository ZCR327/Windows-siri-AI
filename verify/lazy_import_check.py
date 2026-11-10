# -*- coding: utf-8 -*-
"""门禁：requests 必须惰性导入，且**模块内部的裸全局查找必须能用**。

为什么门禁要写成这个样子（2026-11-10 的教训）：
  第一版门禁只做了 llm.requests 这种**外部属性访问** ——
  那恰好是 PEP 562 模块级 __getattr__ 能覆盖的场景，所以门禁全绿；
  但模块**内部函数**里的裸全局 requests.post(...) **根本不走 __getattr__**，
  打包后真实调用直接 NameError: name 'requests' is not defined。
  → 门禁必须在**模块自己的全局命名空间里**执行裸全局查找，
     而且要**真发一次请求**（mock/假代理会把这个坑一起盖住）。

    python tools\\siri_orb\\lazy_import_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = ("https://api.open-meteo.com/v1/forecast"
       "?latitude=31.2&longitude=121.4&current=temperature_2m")
MODULES = ("llm", "tools", "weather", "locate")

PROBE = (
    "def _probe():\n"
    "    r = requests.get(%r, timeout=12)\n"
    "    return r.status_code\n"
) % URL


def child(mod):
    """在子进程里跑：导入模块 -> 看 requests 是否已被导入 -> "
    "在**该模块的全局命名空间**里执行裸全局查找。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    out = {"mod": mod, "loaded_at_import": None, "status": None, "error": None}
    try:
        import importlib
        m = importlib.import_module(mod)
    except Exception as exc:
        out["error"] = "import failed: %r" % (exc,)
        print(json.dumps(out))
        return
    out["loaded_at_import"] = ("requests" in sys.modules)
    ns = {}
    try:
        exec(PROBE, m.__dict__, ns)
        out["status"] = ns["_probe"]()
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(out))


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--probe":
        child(sys.argv[2])
        return 0
    fails = []
    for mod in MODULES:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--probe", mod],
                           capture_output=True, text=True)
        try:
            r = json.loads((p.stdout or "").strip().splitlines()[-1])
        except Exception:
            print("FAIL %-8s no result (stdout=%r stderr=%r)"
                  % (mod, (p.stdout or "")[-200:], (p.stderr or "")[-300:]))
            fails.append(mod)
            continue
        ok_lazy = (r.get("loaded_at_import") is False)
        ok_call = (r.get("status") == 200)
        print("%s %-8s import后未导入 requests=%s  模块内部真请求=%s%s"
              % ("PASS" if (ok_lazy and ok_call) else "FAIL", mod,
                 r.get("loaded_at_import"), r.get("status"),
                 ("  -> " + str(r.get("error"))) if r.get("error") else ""))
        if not (ok_lazy and ok_call):
            fails.append(mod)
    print("")
    if fails:
        print("FAILED: %s" % ", ".join(fails))
        return 1
    print("ALL OK (%d modules)" % len(MODULES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
