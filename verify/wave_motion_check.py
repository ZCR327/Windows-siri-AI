# -*- coding: utf-8 -*-
"""波前运动回归检查：扩散时不能「卡在一个地方」。

2026-11-09 实测出来的 bug：
    半径缓动 ease = 1-(1-tau)^1.7 前快后慢，末端速度趋零。
    波前是屏幕上一个圆，它撞到**离波源最远的那个角（左上角）**之后
    就再也没有东西可以推进了 —— 那个位置之前的实测：
        t=438ms  前沿=1591px（= 最远角）  位移 13.1px
        t=449..493ms（连续 6 帧）         位移 **0.0px**
    也就是说光在前 70% 的时间里冲完，然后**在同一个地方钉死 66ms**，
    用户看到的现象就是「每次扩散都会在一个地方卡一下」。

本检查用**真实代码路径**（Overlay._wave_state + GlowRenderer）量：
    1. 每一帧的前沿位移都必须 > 0（不能有钉死的帧）
    2. 单帧位移不能超过光带宽度（不能「跳着走」）
    3. 波前必须在 sweep 结束前抵达最远角（不能有死时间）
    4. 最后一帧的前沿位移也不能是 0

用法： python tools\\siri_orb\\wave_motion_check.py
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt5 import QtWidgets

import siri_orb
from siri_orb import GlowRenderer

TICK = 0.011          # 覆盖层定时器周期（siri_orb.py: self.timer.start(11)）
FAILS = []


def check(ok, name, detail=""):
    print("%-4s %s%s" % ("OK" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def load_cfg():
    """找配置：**不许写死绝对路径** —— 公开仓库的扫描器会拦盘符（PATH/abs-drive）。

    顺序：环境变量 -> 脚本同级 -> 仓库根。都找不到就用内置默认值，
    默认值已经够这个检查用（波前几何只依赖 glow_scale / glow_origin / 延迟与扫描时长）。
    """
    cands = []
    env = os.environ.get("SIRI_OS_CONFIG")
    if env:
        cands.append(env)
    cands.append(os.path.join(HERE, "config.json"))
    cands.append(os.path.join(HERE, "..", "..", "..", "config.json"))
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def main():
    cfg = load_cfg()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    W, H = 1440, 900
    scale = max(2, int(cfg.get("glow_scale", 6) or 6))
    origin = cfg.get("glow_origin") or [0.9236, 0.9711]
    delay = float(cfg.get("wave_delay_sec", 0.2))
    sweep = float(cfg.get("wave_sweep_sec", 0.5))
    band_w = max(30.0, W * float(cfg.get("wave_band_frac", 0.032)))

    ox, oy = origin[0] * W, origin[1] * H
    gr = GlowRenderer(W, H, scale=scale, origin=(ox, oy))

    ov = siri_orb.Overlay(cfg)          # 只为拿到真实的 _wave_state
    press = time_base = 1000.0

    print("screen=%dx%d  scale=%d  波源=(%.0f,%.0f)  最远角=%.0fpx  光带宽=%.0f"
          % (W, H, scale, ox, oy, gr.max_r, band_w))
    print("delay=%.2fs  sweep=%.2fs  tick=%.0fms" % (delay, sweep, TICK * 1000))
    print("")

    fronts = []
    radii = []
    t = 0.0
    while t < delay + sweep + TICK:
        wave, _gain = ov._wave_state(press + t, press, delay, sweep, gr.max_r, band_w)
        if wave is None:
            break
        R = float(wave.get("radius", 0.0))
        if R < -1e8:
            t += TICK
            continue
        arr = gr.render(t, 1.0, "listening", wave, 0.0, 0.0, u8=True)
        a = arr[..., 3]
        ys, xs = np.nonzero(a > 60)
        if len(xs) == 0:
            front = 0.0
        else:
            d = np.sqrt((xs * scale - ox) ** 2 + (ys * scale - oy) ** 2)
            front = float(d.max())
        fronts.append((t - delay, R, front))
        radii.append(R)
        t += TICK

    if len(fronts) < 5:
        check(False, "取样帧数够（>=5）", "只取到 %d 帧" % len(fronts))
        return 1

    steps = []
    for i in range(1, len(fronts)):
        dt = fronts[i][0] - fronts[i - 1][0]
        d = fronts[i][2] - fronts[i - 1][2]
        steps.append((fronts[i][0], d, dt))

    zero = [s for s in steps if s[1] <= 0.0]
    check(not zero, "没有「位移=0」的钉死帧",
          "有 %d 帧位移为 0（最早 t=%.0fms）" % (len(zero), zero[0][0] * 1000) if zero else
          "最短位移 %.1fpx" % min(s[1] for s in steps))

    jump = max(steps, key=lambda s: s[1])
    check(jump[1] <= band_w * 1.25, "单帧位移不超过光带宽度（不跳着走）",
          "最大 %.1fpx/帧 @ t=%.0fms（光带宽 %.0fpx）" % (jump[1], jump[0] * 1000, band_w))

    # 波前撞到最远角的时刻不能太早 —— 撞早了，剩下的时间就是干等（= 卡）
    corner = gr.max_r
    hit = None
    for (tt, _R, f) in fronts:
        if f >= corner - 1.0:
            hit = tt
            break
    check(hit is not None and hit >= sweep * 0.90,
          "波前不早于 sweep 的 90% 撞到最远角（没有提前到头干等）",
          "撞到时刻 t=%.0fms / sweep=%.0fms" % ((hit if hit is not None else -1) * 1000,
                                                sweep * 1000))

    check(fronts[-1][2] >= corner - 1.0, "末帧前沿贴到最远角",
          "末帧 %.0fpx / 最远角 %.0fpx" % (fronts[-1][2], corner))

    print("")
    print("%-9s %-10s %-10s %-10s" % ("t(ms)", "R", "前沿", "位移"))
    for i in range(0, len(fronts), 3):
        t_ms, R, f = fronts[i]
        d = "" if i == 0 else "%.1f" % (fronts[i][2] - fronts[i - 1][2])
        print("%-9.0f %-10.0f %-10.0f %-10s" % (t_ms * 1000, R, f, d))
    print("")
    if FAILS:
        print("FAILED: %s" % ", ".join(FAILS))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
