# -*- coding: utf-8 -*-
"""把任务栏右下角裁出来，确认按钮正好落在电量与日期之间。"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os, sys
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "t3_03.png")
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "tmp", "tray_check.png")
im = Image.open(src).convert("RGB")
crop = im.crop((1230, 845, 1440, 900)).resize((210 * 4, 55 * 4), Image.LANCZOS)
crop.save(out)
print("saved", out, crop.size)
