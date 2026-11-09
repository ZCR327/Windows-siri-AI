# -*- coding: utf-8 -*-
"""长按手势回归检查（2026-11-09 加）。

用户拍板的行为：
  - 按住小米键 -> **不触发**（按钮先亮 + 进度环当反馈）
  - 按住满 hold_trigger_ms -> **立刻触发**（还按着就开始扩散 + 弹卡片）
  - 不到阈值就松手 -> **什么都不做**（纯防误触）
  - 按住不放时 Windows 的**自动重复 KEYDOWN 不能把计时器重置**
    （按住 0.5 秒会连发几十个 KEYDOWN，计时器被重置就永远触发不了）

跑的是真实代码路径（keyhook.HoldGesture），用真时钟。
用法： python tools\\siri_orb\\hold_trigger_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import keyhook

FAILS = []


def check(ok, name, detail=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def case_short_press():
    fired, started, short = [], [], []
    g = keyhook.HoldGesture(hold_sec=0.30,
                             on_press_start=lambda: started.append(1),
                             on_fire=lambda: fired.append(time.time()),
                             on_short=lambda h: short.append(h))
    g.press()
    time.sleep(0.08)
    g.release()
    time.sleep(0.35)          # 等过阈值，确认不会补触发
    check(not fired, "短按（不到阈值）不触发", "fired=%d" % len(fired))
    check(len(started) == 1, "按下会通知宿主（先亮按钮）", "started=%d" % len(started))
    check(len(short) == 1, "短按会通知宿主收尾", "short=%d" % len(short))
    check(g.holding is False, "松手后不再处于按住状态")


def case_hold_fires():
    fired = []
    t0 = time.time()
    g = keyhook.HoldGesture(hold_sec=0.30, on_fire=lambda: fired.append(time.time()))
    g.press()
    time.sleep(0.45)
    check(len(fired) == 1, "按住满阈值触发一次", "fired=%d" % len(fired))
    if fired:
        dt = fired[0] - t0
        check(0.26 <= dt <= 0.45, "触发时刻≈阈值（不是立刻、也不晚太多）", "dt=%.3fs" % dt)
    g.release()
    time.sleep(0.20)
    check(len(fired) == 1, "松手不会重复触发", "fired=%d" % len(fired))


def case_auto_repeat():
    fired = []
    g = keyhook.HoldGesture(hold_sec=0.30, on_fire=lambda: fired.append(time.time()))
    g.press()
    for _ in range(8):        # Windows 的自动重复 KEYDOWN
        time.sleep(0.03)
        g.press()
    time.sleep(0.20)
    check(len(fired) == 1,
          "自动重复 KEYDOWN 不会把计时器重置（长按仍能触发）", "fired=%d" % len(fired))


def case_zero_hold():
    fired = []
    g = keyhook.HoldGesture(hold_sec=0.0, on_fire=lambda: fired.append(time.time()))
    g.press()
    check(len(fired) == 1, "阈值=0 时按下就触发（旧行为）", "fired=%d" % len(fired))


def case_dynamic_threshold():
    box = [0.20]
    fired = []
    g = keyhook.HoldGesture(hold_sec=lambda: box[0],
                            on_fire=lambda: fired.append(time.time()))
    g.press()
    time.sleep(0.32)
    check(len(fired) == 1, "阈值是函数时按当前值生效（设置面板改完立刻见效）",
          "fired=%d" % len(fired))


def main():
    print("长按手势回归检查")
    print("")
    for fn in (case_short_press, case_hold_fires, case_auto_repeat,
               case_zero_hold, case_dynamic_threshold):
        fn()
        print("")
    if FAILS:
        print("FAILED: %s" % ", ".join(FAILS))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
