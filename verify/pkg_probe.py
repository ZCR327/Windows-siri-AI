# -*- coding: utf-8 -*-
"""量一量打包出来的 exe：装了多少东西、解包要多久、启动能快多少。

为什么要有这个工具：
  助手启动慢的大头是 **onefile 每次解包**，不是 Python 本身。
  实测：exe 启动 -> 界面就绪 2.65s，其里 import 只要 0.57s，
  解包（134MB / 240 个文件）≈ 1.9s。
  所以「启动快不快」得先量清楚**包里到底装了什么**，而不是凭感觉调参。

用法：
    python tools\\siri_orb\\pkg_probe.py <Siri-OS.exe>

输出（纯 ASCII，方便贴进日志）：
    exe_bytes / entries / unpack_bytes / unpack_files / stored_ratio
    extract_sec        实测把这套东西解出来写磁盘要几秒
    est_startup_sec    估算启动到界面就绪 = 解包 + import + Qt/预热
    top               包里最大的几个文件（看还能再剪进何处）
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import shutil
import sys
import tempfile
import time

# 打包版的固定开销（2026-11-10 实测）：
IMPORT_SEC = 0.57      # import siri_orb（numpy / PyQt5 / win32com / PIL 都在里面）
QT_WARMUP_SEC = 0.14   # QApplication + overlay.show() + warmup()（首帧预热）


def main(argv):
    if len(argv) < 2:
        print("usage: pkg_probe.py <Siri-OS.exe>")
        return 2
    path = argv[1]
    if not os.path.exists(path):
        print("no such exe: %s" % path)
        return 2
    from PyInstaller.archive.readers import CArchiveReader

    t0 = time.time()
    arc = CArchiveReader(path)
    toc = arc.toc
    n = len(toc)
    unpack_bytes = sum(int(v[2]) for v in toc.values())
    stored_bytes = sum(int(v[1]) for v in toc.values())
    exe_bytes = os.path.getsize(path)

    tmp = tempfile.mkdtemp(prefix="pkgprobe_")
    files = 0
    on_disk = 0
    try:
        t1 = time.time()
        for name, entry in toc.items():
            data = arc.extract(name)
            out = os.path.join(tmp, name.replace("\\", os.sep).replace("/", os.sep))
            d = os.path.dirname(out)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
            files += 1
            on_disk += len(data)
        extract_sec = time.time() - t1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("exe: %s" % path)
    print("exe_bytes: %d" % exe_bytes)
    print("entries: %d" % n)
    print("unpack_bytes: %d" % unpack_bytes)
    print("unpack_mb: %.2f" % (unpack_bytes / 1048576.0))
    print("unpack_files: %d" % files)
    print("stored_ratio: %.3f" % (float(exe_bytes) / max(1, unpack_bytes)))
    print("extract_sec: %.3f" % extract_sec)
    print("on_disk_mb: %.2f" % (on_disk / 1048576.0))
    print("est_startup_sec: %.3f" % (extract_sec + IMPORT_SEC + QT_WARMUP_SEC))
    print("probe_total_sec: %.3f" % (time.time() - t0))

    big = sorted(toc.items(), key=lambda kv: int(kv[1][2]), reverse=True)[:12]
    print("top:")
    for name, entry in big:
        print("  %8.2f MB  %s" % (int(entry[2]) / 1048576.0, name))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
