# -*- coding: utf-8 -*-
"""打包成 exe：python tools\siri_orb\build_exe.py

做四件事：
  1) 检查 PyInstaller 在不在（不在就打印安装命令）；
  2) 生成图标 siri_os.ico（没有就现画一个）；
  3) 跑 PyInstaller，产出 dist/Siri-OS.exe；
  4) 把 exe 收进 release/siri-os-exe/，旁边放一份使用说明。

⚠️ 这一步会**改本机环境**（需要 pip install pyinstaller）。
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPEC = os.path.join(HERE, "siri_os.spec")
ICON = os.path.join(HERE, "siri_os.ico")
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
OUT_DIR = os.path.join(ROOT, "release", "siri-os-exe")

README = """Siri-OS 桌面语音助手（Windows 免安装版）
==========================================

怎么用
------
1. 把 Siri-OS.exe 放在一个**你自己有写权限**的目录（比如用户目录下的 `Siri-OS` 文件夹）。
   同目录会自动生成：config.json（设置）、config/api_keys.json（密钥）、logs/（日志）。
2. 双击运行。右下角托盘出现图标 = 已经在跑了。
3. 按小米笔记本上的「小米语音助手键」唤醒（这台机器是那个键；
   别的机器可以在设置里改兜底热键，默认 ctrl+alt+space）。
4. 右键托盘图标 -> 设置… 可以改后端 / 模型 / 助理能力 / 上下文。

密钥
----
设置面板 -> 「模型」页，每家厂商一个密钥输入框，填了保存即写入
config/api_keys.json（这个文件不要发给别人）。

出问题了看哪儿
--------------
logs\siri_orb.log   助手的运行日志（打包后没有控制台，日志都在这儿）
logs\keyhook.log    按键日志

托盘里没有图标 / 双击没反应
---------------------------
多半是被杀毒软件拦了（未签名的 exe 容易被误报），把它加进白名单再试。

第一次启动会慢一两秒 —— 单文件 exe 要先解包到临时目录，这是正常的。
"""


def make_icon(path):
    """画一个 Siri 风格的小图标（深色圆底 + 虹彩光晕环）。"""
    try:
        import colorsys
        from PIL import Image, ImageDraw, ImageFilter
    except Exception as e:
        print("Pillow missing, skip icon: %s" % e)
        return False
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i in range(360):
        h = i / 360.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.55, 1.0)
        d.arc((10, 10, S - 10, S - 10), i, i + 2,
              fill=(int(r * 255), int(g * 255), int(b * 255), 235), width=15)
    img = Image.alpha_composite(img.filter(ImageFilter.GaussianBlur(9)), img)
    d = ImageDraw.Draw(img)
    d.ellipse((78, 78, S - 78, S - 78), fill=(16, 17, 23, 255))
    d.ellipse((98, 98, S - 98, S - 98), fill=(44, 46, 64, 255))
    img.save(path, format="ICO", sizes=[(256, 256), (64, 64), (48, 48),
                                        (32, 32), (16, 16)])
    return True


def main():
    try:
        import PyInstaller
    except Exception:
        print("PyInstaller is not installed. Run:")
        print("    python -m pip install pyinstaller")
        return 2
    print("PyInstaller %s" % PyInstaller.__version__)

    if not os.path.exists(ICON) and not make_icon(ICON):
        print("no icon (build continues)")

    for d in (DIST, BUILD):
        shutil.rmtree(d, ignore_errors=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", SPEC]
    print("running: %s" % " ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode != 0:
        print("PyInstaller failed rc=%s" % r.returncode)
        return r.returncode

    exe = os.path.join(DIST, "Siri-OS.exe")
    if not os.path.exists(exe):
        print("build finished but exe is missing: %s" % exe)
        return 1

    if os.path.isdir(OUT_DIR):
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    shutil.copy2(exe, os.path.join(OUT_DIR, "Siri-OS.exe"))
    with open(os.path.join(OUT_DIR, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README)

    mb = os.path.getsize(exe) / 1048576.0
    print("OK: %s (%.1f MB)" % (exe, mb))
    print("collected to: %s" % OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
