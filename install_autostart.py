# -*- coding: utf-8 -*-
"""开机自启安装/卸载/查看。

安装位置：当前用户的「启动」文件夹快捷方式（不需要管理员权限，
任务管理器 -> 启动 里也能看到、随时能关）。

    python tools\siri_orb\install_autostart.py install
    python tools\siri_orb\install_autostart.py remove
    python tools\siri_orb\install_autostart.py status
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISOR = os.path.join(HERE, "startup.py")
SHORTCUT_NAME = "Siri-OS 语音助手.lnk"

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

# 打包成 exe：开机自启直接指向 exe 自己，不再走 pythonw + startup.py 守护
FROZEN = bool(pathmod is not None and pathmod.is_frozen())
EXE = pathmod.exe_path() if FROZEN else None


# 会话工作区是**临时目录**（会话结束就没了）。
# 指到那里的自启有两个问题：① 会话一结束快捷方式就指向不存在的文件；
# ② 它启动的是**另一份程序、读另一份 config.json** ——
#    表现就是「每次电脑重启后配置都被重置」（2026-11-08 本机真踩过）。
_TEMP_HINTS = ("\\sessions\\", "/sessions/")


def is_temp_workspace(path):
    """这个路径是不是在会话临时目录里。"""
    p = (path or "").lower().replace("/", "\\")
    return any(h in p for h in _TEMP_HINTS)


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def pythonw():
    """优先用 pythonw.exe：没有控制台窗口，不打扰人。"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    return exe


def shortcut_path():
    return os.path.join(startup_dir(), SHORTCUT_NAME)


def is_installed():
    """开机自启快捷方式在不在（设置面板拿它显示真实状态）。"""
    return os.path.exists(shortcut_path())


def install():
    d = startup_dir()
    if not os.path.isdir(d):
        os.makedirs(d)
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
    except Exception as e:
        print("need pywin32 for .lnk creation: %s" % e)
        return 2
    lnk = shell.CreateShortCut(shortcut_path())
    if FROZEN:
        lnk.Targetpath = EXE
        lnk.Arguments = ""
        lnk.WorkingDirectory = pathmod.data_dir()
    else:
        if is_temp_workspace(HERE):
            print("!! 这个路径在**会话临时目录**里：%s" % HERE)
            print("   指过去的自启有两个毛病：")
            print("   1) 会话结束路径就没了 -> 快捷方式指向不存在的文件；")
            print("   2) 它启动的是**另一份程序、读另一份 config.json** ->")
            print("      表现就是「每次电脑重启后配置都被重置」。")
            print("   正确做法：在**打包好的 exe** 里打开设置面板，打开「开机自启」。")
            return 2
        lnk.Targetpath = pythonw()
        lnk.Arguments = '"%s"' % SUPERVISOR
        lnk.WorkingDirectory = HERE
    lnk.Description = "Siri-OS 桌面语音助手（常驻守护）"
    lnk.WindowStyle = 7
    lnk.save()
    print("installed: %s" % shortcut_path())
    print("  target : %s" % lnk.Targetpath)
    print("  args   : %s" % lnk.Arguments)
    return 0


def remove():
    p = shortcut_path()
    if os.path.exists(p):
        os.remove(p)
        print("removed: %s" % p)
    else:
        print("not found: %s" % p)
    return 0


def status():
    p = shortcut_path()
    print("startup dir : %s" % startup_dir())
    print("shortcut    : %s (%s)" % (p, "EXISTS" if os.path.exists(p) else "MISSING"))
    if os.path.exists(p):
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            lnk = shell.CreateShortCut(p)
            print("  target    : %s" % lnk.Targetpath)
            print("  arguments : %s" % lnk.Arguments)
            print("  workdir   : %s" % lnk.WorkingDirectory)
            if is_temp_workspace(lnk.Targetpath) or is_temp_workspace(lnk.Arguments):
                print("  !! 指向会话临时目录 —— 重启后会启动另一份程序（配置看起来被重置）")
            elif FROZEN and lnk.Targetpath and os.path.normcase(lnk.Targetpath) != os.path.normcase(EXE or ""):
                print("  !! 指向的不是本 exe（%s）" % EXE)
        except Exception as e:
            print("  (cannot read: %s)" % e)
    pidf = os.path.join((pathmod.data_dir() if pathmod else HERE), "logs", "supervisor.pid")
    if os.path.exists(pidf):
        try:
            with open(pidf, "r", encoding="utf-8") as f:
                pid = f.read().strip()
            print("supervisor pid file: %s" % pid)
        except Exception:
            pass
    if FROZEN:
        # 打包后没有守护进程，助手自己就是那个进程
        try:
            import cfgstore
            p = cfgstore.resident_pid()
            print("resident pid: %s" % (p if p else "(not running)"))
        except Exception:
            pass
    return 0


def start():
    """立刻拉起守护（不装自启也能用）。"""
    import subprocess
    if FROZEN:
        subprocess.Popen([EXE], cwd=pathmod.data_dir(),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("started %s" % EXE)
        return 0
    subprocess.Popen([pythonw(), SUPERVISOR], cwd=HERE,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("started supervisor")
    return 0


def stop():
    """停掉守护及其子进程。"""
    import subprocess
    if FROZEN:
        try:
            import cfgstore
            pid = cfgstore.resident_pid()
        except Exception:
            pid = None
        if not pid:
            print("assistant is not running")
            return 0
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        print("stopped assistant pid=%d" % pid)
        return 0
    pidf = os.path.join((pathmod.data_dir() if pathmod else HERE), "logs", "supervisor.pid")
    pid = 0
    try:
        with open(pidf, "r", encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        pid = 0
    if not pid:
        print("no supervisor pid file")
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)
    print("stopped supervisor pid=%d (and children)" % pid)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = {"install": install, "remove": remove, "status": status,
          "start": start, "stop": stop}.get(cmd)
    if fn is None:
        print("usage: install_autostart.py [install|remove|start|stop|status]")
        sys.exit(2)
    sys.exit(fn())
