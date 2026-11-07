# -*- coding: utf-8 -*-
"""Siri-OS 常驻守护：拉起 siri_orb.py，崩了就自动重拉。

为什么需要它：直接跑 siri_orb.py 的话，进程一挂就没了；
开机自启指向这个守护，而不是直接指向 siri_orb.py。

用法（一般由开机启动项调用，不用手动跑）：
    python tools\siri_orb\startup.py
"""
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOG_DIR, "supervisor.log")
# 子进程的 stdout/stderr 落盘。pythonw.exe 没有控制台，
# 不重定向的话 Python 的 traceback 会直接丢掉 —— 渲染层一旦抛异常就完全查不到。
CHILD_LOG = os.path.join(LOG_DIR, "siri_orb.log")
PIDFILE = os.path.join(LOG_DIR, "supervisor.pid")
SCRIPT = os.path.join(HERE, "siri_orb.py")
RESTART_DELAY = 3.0


def log(msg):
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _child_flags():
    """父进程没有控制台时（开机自启走的是 pythonw.exe），子进程必须 CREATE_NO_WINDOW，
    否则 Windows 会给它新建一个控制台窗口。手动在终端里跑时不需要（子进程继承即可）。"""
    try:
        import ctypes
        if ctypes.windll.kernel32.GetConsoleWindow():
            return 0
    except Exception:
        pass
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pid_alive(pid):
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def another_supervisor_running():
    try:
        with open(PIDFILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        return False
    if pid and pid != os.getpid() and pid_alive(pid):
        return True
    return False


def main():
    if another_supervisor_running():
        log("supervisor already running -> exit")
        return 0
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    log("supervisor start pid=%d python=%s" % (os.getpid(), sys.executable))

    child = None
    started_at = 0.0
    backoff = RESTART_DELAY
    while True:
        if child is None or child.poll() is not None:
            if child is not None:
                uptime = time.time() - started_at
                # 起来不到 10 秒就挂 = 崩溃循环，退避重试，别空转刷屏
                if uptime < 10.0:
                    backoff = min(backoff * 2, 60.0)
                else:
                    backoff = RESTART_DELAY
                log("siri_orb exited rc=%s uptime=%.1fs -> restart in %.1fs"
                    % (child.returncode, uptime, backoff))
                time.sleep(backoff)
            try:
                env = dict(os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                # ⚠️ 必须无缓冲：stdout 重定向到文件后是**块缓冲**，常驻进程永不退出
                # -> 缓冲区永远不刷 -> 日志文件一直是空的（2026-11-07 实测踩过）。
                env["PYTHONUNBUFFERED"] = "1"
                # 二进制追加：子进程写什么字节就存什么字节，不做编码转换
                out = open(CHILD_LOG, "ab")
                try:
                    child = subprocess.Popen([sys.executable, "-u", SCRIPT], cwd=HERE, env=env,
                                             stdout=out, stderr=subprocess.STDOUT,
                                             creationflags=_child_flags())
                finally:
                    out.close()
                started_at = time.time()
                log("launched siri_orb pid=%d" % child.pid)
            except Exception as e:
                log("launch failed: %s" % e)
                child = None
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
