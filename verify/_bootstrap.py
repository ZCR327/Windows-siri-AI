# -*- coding: utf-8 -*-
# 让 verify/ 里的脚本能 import 到仓库根目录的模块（siri_orb / cfgstore ...）。
# 不用改任何脚本源码：每个 verify 脚本第一行 import 本文件。
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
