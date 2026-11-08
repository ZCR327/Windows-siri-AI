# -*- coding: utf-8 -*-
"""路径解析：源码跑 vs 打包成 exe，两种情况路径完全不一样。

打包成 exe（PyInstaller）之后有两套目录：
  - **解包目录**（sys._MEIPASS）：只读，里面是代码和资源（listen.ps1 等）；
  - **exe 同级目录**：可写，配置 / 密钥库 / 日志必须待在这儿。

源码运行时两者是同一个目录（脚本所在目录）。

⚠️ 以前所有路径都是硬编码 os.path.dirname(__file__)，
   打包后会写到解包目录里（temp 目录）—— 关掉程序配置就没了。
"""
import os
import sys


def is_frozen():
    """是不是打包成 exe 在跑。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir():
    """只读资源目录：脚本所在目录（源码） / 解包目录（打包后）。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """可写数据目录：脚本所在目录（源码） / exe 同级目录（打包后）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def exe_path():
    """正在运行的 exe 路径（打包后），源码模式返回 None。"""
    if not is_frozen():
        return None
    return os.path.abspath(sys.executable)
