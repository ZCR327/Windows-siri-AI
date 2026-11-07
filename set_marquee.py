# -*- coding: utf-8 -*-
"""一键调跑马灯速度：写进 config.json，常驻实例 1 秒内自动生效（不用重启）。

用法：
    python tools\siri_orb\set_marquee.py 6      # 一圈 6 秒
    python tools\siri_orb\set_marquee.py 6 0.09 # 顺带调尾巴长度（圈的比例）
    python tools\siri_orb\set_marquee.py show   # 只看当前值
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "config.json")


def show(cfg):
    p = float(cfg.get("marquee_period_sec", 6.0))
    tr = float(cfg.get("marquee_trail", 0.09))
    print("marquee_period_sec = %.2f  (跑一圈几秒)" % p)
    print("marquee_trail      = %.3f  (尾巴长度, 圈的比例 -> %.0f 度)" % (tr, tr * 360.0))
    print("   全程恒定（状态之间不变速）")


def main():
    cfg = json.load(io.open(CFG, "r", encoding="utf-8"))
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args or args[0] == "show":
        show(cfg)
        return 0
    try:
        per = float(args[0])
    except ValueError:
        print("usage: set_marquee.py <seconds> [trail]")
        return 1
    if per < 1.0:
        print("too fast: %s (min 1.0)" % per)
        return 1
    cfg["marquee_period_sec"] = per
    if len(args) > 1:
        cfg["marquee_trail"] = float(args[1])
    with io.open(CFG, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        f.write("\n")
    print("saved. running instance picks it up within ~1s.")
    show(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
