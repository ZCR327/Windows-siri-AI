# -*- coding: utf-8 -*-
"""抓一张按键后的字幕卡，确认「麦克风异常」提示是否真的出现。

用法： python tools\siri_orb\caption_check.py [等秒数]
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import time

from PIL import ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import inject_scan

wait = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

inject_scan.send_scan(0x72)
time.sleep(wait)
im = ImageGrab.grab()
out = os.path.join(HERE, "caption_check.png")
im.crop((0, 0, im.width, 320)).save(out)
print("wrote %s  (%dx%d)" % (out, im.width, 320))
