# -*- mode: python ; coding: utf-8 -*-
"""Siri-OS 打包脚本（PyInstaller）—— 产出**单文件** exe，双击就能跑，不用装 Python。

单文件的取舍：
  - 好处：用户只拿到一个 exe，双击即用，零依赖零安装；
  - 代价：每次启动要把包解到临时目录（慢一两秒），常驻助手无所谓。

⚠️ 打包后有两套目录（见 paths.py）：
  - **解包目录**（sys._MEIPASS）：只读，放代码和 listen.ps1；
  - **exe 同级目录**：可写，放 config.json / 密钥库 / 日志。
  写错地方的话，关掉程序配置就没了 —— 所以 paths.data_dir() 才是落盘的地方。

用法：
    python tools\siri_orb\build_exe.py
"""
import os

ROOT = os.path.abspath(SPECPATH)
ICON = os.path.join(ROOT, "siri_os.ico")

# 只读资源：听写脚本（voice.py 会 powershell -File 它）
# probe_proc.ps1 是 doctor 的进程探针 —— 少了它，doctor 在打包版里看不到自己
datas = [(os.path.join(ROOT, "listen.ps1"), "."),
         (os.path.join(ROOT, "probe_proc.ps1"), ".")]

# 隐式导入：这些模块是运行时动态拿的，PyInstaller 静态扫不出来
hiddenimports = [
    "win32api", "win32clipboard", "win32con", "win32gui", "win32process",
    "win32com", "win32com.client", "pythoncom", "pywintypes",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageGrab",
    "numpy",
    "requests", "urllib3", "certifi", "charset_normalizer", "idna",
]

# 砍掉用不上的：cv2 只有 ref_*.py（开发期逆向参考视频）用得到，
# 助手本体不碰 —— 带上会让 exe 大出好几倍。
excludes = [
    "cv2", "matplotlib", "scipy", "pandas", "tkinter", "pytest", "IPython",
    "notebook", "jupyter", "setuptools", "pip", "wheel",
    "PyQt5.QtWebEngineCore", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebChannel",
    "PyQt5.QtMultimedia", "PyQt5.QtMultimediaWidgets", "PyQt5.QtQml",
    "PyQt5.QtQuick", "PyQt5.Qt3DCore", "PyQt5.QtBluetooth",
    "PyQt5.QtPositioning", "PyQt5.QtSensors", "PyQt5.QtSerialPort",
    "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtCharts", "PyQt5.QtPdf",
    "PyQt5.QtDesigner", "PyQt5.QtHelp", "PyQt5.QtOpenGL",
]

a = Analysis(
    [os.path.join(ROOT, "siri_orb.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Siri-OS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # 托盘常驻，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.exists(ICON) else None,
)
