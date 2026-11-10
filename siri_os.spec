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
         (os.path.join(ROOT, "probe_proc.ps1"), "."),
         (os.path.join(ROOT, "locate.ps1"), ".")]

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
    # ---- 下面三个是**实测**确认的可选依赖（2026-11-10）----
    # 拦掉它们之后 numpy 正常、requests 正常、真实 HTTPS 请求 200，
    # 且运行时 sys.modules 里根本没出现它们的名字 —— 纯负担。
    "cryptography",   # requests/urllib3 不在运行时用它
    "zstandard",      # urllib3 的可选 zstd 解码分支
    "yaml",           # numpy.__config__ 的可选分支
    "win32ui",        # MFC 壳，win32com 用的是 pythoncom/pywintypes
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

# ---------------------------------------------------------------- 裁剪死重
# 助手本体只走 QPainter 光栅引擎（没有 QOpenGLWidget / QQuickWidget），
# 网络走 requests（不用 QtNetwork），界面文案自己管（i18n.py）。
# 下面这些是「装进去但永远用不到」的死重，加起来 **53MB**：
#   opengl32sw.dll     Mesa 软件 OpenGL —— 只有建 QOpenGLContext 才加载
#   d3dcompiler_47     D3D 着色器编译 —— 只有 ANGLE/QtQuick 用
#   Qt5Quick/Qt5Qml    QML 引擎 —— 项目里没有任何 .qml
#   libGLESv2/libEGL   ANGLE —— 同上
#   Qt5Network         网络走 requests
#   Qt5DBus            Linux 的 D-Bus
#   translations/*.qm  75 个语言包 4.9MB —— 文案我们自己管
# ⚠️ 删之前用 PE 导入表核过（不是拍脑袋）：Qt5Gui.dll **不**静态依赖上面任何一个
#    dll，Qt5Widgets.dll 也不依赖 Qt5Quick/Qt5Qml/Qt5Network。
#    被删的插件（qwebgl 依赖 ANGLE，qsvg/qsvgicon 依赖 Qt5Svg）跟着一起走。
TRIM_FILES = (
    "opengl32sw.dll", "d3dcompiler_47.dll",
    "Qt5Quick.dll", "Qt5Qml.dll", "Qt5QmlModels.dll",
    "libGLESv2.dll", "libEGL.dll",
    "Qt5Network.dll", "Qt5DBus.dll", "Qt5Svg.dll", "Qt5WebSockets.dll",
    "qwebgl.dll", "qsvg.dll", "qsvgicon.dll",
    "qtuiotouchplugin.dll", "qxdgdesktopportal.dll",
)
TRIM_DIRS = ("PyQt5\\Qt5\\translations",)


def _trim(items):
    """按文件名/目录裁掉死重。

    ⚠️ PyInstaller 6 的条目分两种形状：
       binaries 是 (src, dest_dir, typecode) 三元组，dest **是目录**；
       datas 是 (src, dest_path) 二元组，dest 是**完整路径**。
       所以文件名要 src 和 dest 的 basename **都看**，
       容器也要保留原来的元组长度。
    """
    out = []
    for item in items:
        src = str(item[0])
        rel = str(item[1]).replace("/", "\\")
        names = (os.path.basename(src), os.path.basename(rel))
        if any(n in TRIM_FILES for n in names):
            continue
        if any(rel.startswith(p) for p in TRIM_DIRS):
            continue
        out.append(item)
    return out


a.binaries = _trim(a.binaries)
a.datas = _trim(a.datas)

pyz = PYZ(a.pure)

# ⚠️ **onedir（文件夹版），不是 onefile（2026-11-10 改）：
#   onefile 每次启动都要把 89MB 解到 %TEMP% —— 实测解包就占掉 1.4~1.9s，
#   是「双击到能用」的全部大头。onedir 直接从文件夹里跑，解包这一步整个消失：
#   实测 A/B：2.12s -> **0.31s**（同机同口径）。
#   代价：发布物是一个文件夹，不是「一个 exe」。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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

# 一个可以直接跑的文件夹：exe + _internal/（库、插件、资源都在里面）
coll = COLLECT(exe, a.binaries, a.datas,
               strip=False, upx=False, upx_exclude=[],
               name="Siri-OS")
