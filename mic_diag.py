# -*- coding: utf-8 -*-
"""麦克风采集路径诊断（计划 1.2）。

要回答的问题：**到底是「设备/驱动坏了」，还是「我们的采集路径选错了设备」？**

已知事实（2026-11-08 22:5x 实测）：
  - Win32_SoundDevice 里 4 个设备状态全是 OK；
  - Audiosrv / AudioEndpointBuilder 都在 Running；
  - 但 winmm（waveIn + WAVE_MAPPER）连采 20 块 50ms，**rms/peak 全是精确的 0**，
    而同时扬声器正在放 Alarm01.wav —— 这不是「环境安静」能解释的
    （真麦克风在安静房间也会有底噪，不会是整 0）。

最可能的原因：**WAVE_MAPPER 选错了设备**。
waveInOpen(WAVE_MAPPER) 在多设备机器上可能落到「只有播放、没有采集」的端点上
（蓝牙耳机 / 扬声器），那个端点开得起来但永远给静音。

所以这个脚本**逐个枚举 wave-in 设备**，每个都真采一段，报 rms/dbFS/peak/非零样本数，
再跟 Windows 自己的默认录音设备（注册表 Sound Mapper）对一下。

用法：
    python tools\\siri_orb\\mic_diag.py            # 每个设备采 2 秒
    python tools\\siri_orb\\mic_diag.py --play     # 采的同时扬声器放测试音
    python tools\\siri_orb\\mic_diag.py --mapper   # 额外单独测 WAVE_MAPPER
"""
import array
import ctypes
import ctypes.wintypes as wt
import math
import os
import sys
import time

WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
SAMPLE_RATE = 16000
BLOCK_MS = 50

FAIL = []
OK = []


def out(s):
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "backslashreplace").decode("ascii"))


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    out("%s %s%s" % ("PASS" if cond else "FAIL", name,
                     (" -- " + detail) if detail else ""))


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wt.WORD), ("nChannels", wt.WORD),
        ("nSamplesPerSec", wt.DWORD), ("nAvgBytesPerSec", wt.DWORD),
        ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD),
        ("cbSize", wt.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.POINTER(ctypes.c_ubyte)), ("dwBufferLength", wt.DWORD),
        ("dwBytesRecorded", wt.DWORD), ("dwUser", ctypes.c_void_p),
        ("dwFlags", wt.DWORD), ("dwLoops", wt.DWORD),
        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p),
    ]


class WAVEINCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wt.WORD), ("wPid", wt.WORD), ("vDriverVersion", wt.DWORD),
        ("szPname", wt.WCHAR * 32), ("dwFormats", wt.DWORD),
        ("wChannels", wt.WORD), ("wReserved1", wt.WORD),
    ]


def _api():
    w = ctypes.WinDLL("winmm.dll")
    w.waveInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
                             ctypes.POINTER(WAVEFORMATEX), wt.DWORD,
                             wt.DWORD, wt.DWORD]
    w.waveInOpen.restype = ctypes.c_uint
    for fn in ("waveInStart", "waveInReset", "waveInStop", "waveInClose"):
        getattr(w, fn).argtypes = [ctypes.c_void_p]
        getattr(w, fn).restype = ctypes.c_uint
    for fn in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
        getattr(w, fn).argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
        getattr(w, fn).restype = ctypes.c_uint
    w.waveInGetNumDevs.restype = wt.UINT
    w.waveInGetDevCapsW.argtypes = [wt.UINT, ctypes.POINTER(WAVEINCAPS), wt.UINT]
    w.waveInGetDevCapsW.restype = ctypes.c_uint
    return w


def list_devices(w):
    n = w.waveInGetNumDevs()
    rows = []
    for i in range(n):
        caps = WAVEINCAPS()
        rc = w.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        if rc != 0:
            rows.append((i, "<caps rc=%d>" % rc, ""))
            continue
        rows.append((i, caps.szPname, "%dch/0x%08X" % (caps.wChannels, caps.dwFormats)))
    return rows


def probe(w, device_id, secs=2.0, block_ms=BLOCK_MS, rate=SAMPLE_RATE,
          channels=1, bits=16):
    """开一个设备采一段，返回统计字典。

    ⚠️ 格式很重要：这个设备是**2 声道**的（麦克风阵列），
    我们以前死用 16k/单声道 —— 有些驱动不匹配时**不报错，直接给静音**。
    所以这里要把 (采样率, 声道, 位深) 都做成可扫的。
    """
    hIn = ctypes.c_void_p()
    align = channels * bits // 8
    fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, channels, rate,
                        rate * align, align, bits, 0)
    rc = w.waveInOpen(ctypes.byref(hIn), device_id, ctypes.byref(fmt), 0, 0, CALLBACK_NULL)
    if rc != 0:
        return {"open": False, "rc": rc}
    nbytes = rate * align * block_ms // 1000
    buf = (ctypes.c_ubyte * nbytes)()
    hdr = WAVEHDR()
    hdr.lpData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    hdr.dwBufferLength = nbytes
    best = {"rms": 0.0, "db": -99.0, "peak": 0, "nz": 0, "total": 0, "blocks": 0}
    try:
        end = time.time() + secs
        while time.time() < end:
            hdr.dwFlags = 0
            hdr.dwBytesRecorded = 0
            w.waveInPrepareHeader(hIn, ctypes.byref(hdr), ctypes.sizeof(hdr))
            # ⚠️⚠️ 必须 waveInAddBuffer —— 漏了它缓冲区就**从没进过输入队列**，
            #     驱动不会去填，WHDR_DONE 永不置位，我们就只会读到一整块零。
            #     （2026-11-08 实测：漏了它 = 连续 20 块全 0；加上 = 立刻有信号）
            rc = w.waveInAddBuffer(hIn, ctypes.byref(hdr), ctypes.sizeof(hdr))
            if rc != 0:
                w.waveInUnprepareHeader(hIn, ctypes.byref(hdr), ctypes.sizeof(hdr))
                raise RuntimeError("waveInAddBuffer rc=%d" % rc)
            w.waveInStart(hIn)
            deadline = time.time() + block_ms / 1000.0 + 0.5
            while not (hdr.dwFlags & WHDR_DONE):
                if time.time() > deadline:
                    break
                time.sleep(0.003)
            w.waveInStop(hIn)
            # dwBytesRecorded 要在 reset 之前读 —— reset 会把它清零
            n = hdr.dwBytesRecorded or nbytes
            w.waveInReset(hIn)
            w.waveInUnprepareHeader(hIn, ctypes.byref(hdr), ctypes.sizeof(hdr))
            data = bytes(bytearray(buf[:n]))
            cnt = len(data) // align
            if cnt == 0:
                continue
            peak = 0
            acc = 0
            nz = 0
            if bits == 16:
                s = array.array("h")
                s.frombytes(data[:cnt * 2])
                vals = s
                full = 32768.0
            else:
                s = array.array("i")
                s.frombytes(data[:cnt * 4])
                vals = s
                full = 2147483648.0
            for v in vals:
                a = v if v >= 0 else -v
                if a > peak:
                    peak = a
                acc += v * v
                if v:
                    nz += 1
            rms = math.sqrt(float(acc) / cnt) / full
            best["blocks"] += 1
            best["total"] += cnt
            best["nz"] += nz
            if peak > best["peak"]:
                best["peak"] = peak
            if rms > best["rms"]:
                best["rms"] = rms
    finally:
        try:
            w.waveInClose(hIn)
        except Exception:
            pass
    best["db"] = 20.0 * math.log10(best["rms"]) if best["rms"] > 1e-7 else -99.0
    best["open"] = True
    best["rc"] = 0
    return best


def registry_default_capture():
    """Windows 自己记的默认录音设备名（Sound Mapper）。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Multimedia\Sound Mapper")
        for name in ("Capture", "Record"):
            try:
                v, _ = winreg.QueryValueEx(k, name)
                return name, str(v)
            except OSError:
                continue
    except Exception:
        pass
    return None, ""


def play_tone(duration):
    """扬声器放系统测试音 —— 用来证明「麦克风本该听得到东西」。"""
    import threading
    import winsound
    cands = [r"C:\Windows\Media\Alarm01.wav",
             r"C:\Windows\Media\WindowsNotify.wav",
             r"C:\Windows\Media\Ring01.wav"]
    snd = next((c for c in cands if os.path.exists(c)), None)
    if not snd:
        return None
    def go():
        end = time.time() + duration
        while time.time() < end:
            winsound.PlaySound(snd, winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            time.sleep(1.2)
    th = threading.Thread(target=go, daemon=True)
    th.start()
    return snd


def main():
    secs = 2.0
    args = sys.argv[1:]
    if "--secs" in args:
        try:
            secs = float(args[args.index("--secs") + 1])
        except Exception:
            secs = 2.0
    do_play = "--play" in args
    do_mapper = "--mapper" in args
    do_matrix = "--matrix" in args

    # (采样率, 声道, 位深) —— 设备是 2 声道阵列，原生率多半是 48k
    FORMATS = [(16000, 1, 16), (16000, 2, 16), (44100, 1, 16), (44100, 2, 16),
               (48000, 1, 16), (48000, 2, 16), (48000, 2, 32)]

    w = _api()
    out("=== winmm wave-in devices ===")
    devs = list_devices(w)
    out("device count = %d" % len(devs))
    for i, name, fmt in devs:
        out("  [%d] %-40s %s" % (i, name, fmt))

    tag, defname = registry_default_capture()
    out("")
    out("=== windows default capture (registry Sound Mapper) ===")
    out("  %s = %s" % (tag or "(none)", defname or "(none)"))

    if do_play:
        snd = play_tone(secs + 3)
        out("")
        out("=== playing test tone: %s ===" % (snd or "(no wav found)"))

    results = []
    out("")
    out("=== probing each device (%0.1fs each) ===" % secs)
    for i, name, fmt in devs:
        st = probe(w, i, secs)
        if not st.get("open"):
            out("  [%d] %-40s OPEN FAILED rc=%s" % (i, name, st.get("rc")))
            results.append((i, name, None))
            continue
        out("  [%d] %-40s rms=%.5f dbFS=%6.1f peak=%6d nz=%d/%d blocks=%d"
            % (i, name, st["rms"], st["db"], st["peak"], st["nz"],
               st["total"], st["blocks"]))
        results.append((i, name, st))

    matrix_alive = []
    if do_matrix:
        out("")
        out("=== format matrix ===")
        for i, name, _f in devs:
            for (rate, ch, bits) in FORMATS:
                st = probe(w, i, secs, rate=rate, channels=ch, bits=bits)
                if not st.get("open"):
                    out("  [%d] %6dHz %dch %2dbit  OPEN FAILED rc=%s"
                        % (i, rate, ch, bits, st.get("rc")))
                    continue
                alive = st["peak"] > 0
                if alive:
                    matrix_alive.append((i, name, rate, ch, bits, st))
                out("  [%d] %6dHz %dch %2dbit  rms=%.5f dbFS=%6.1f peak=%6d nz=%d/%d %s"
                    % (i, rate, ch, bits, st["rms"], st["db"], st["peak"],
                       st["nz"], st["total"], "<== ALIVE" if alive else ""))

    if do_mapper:
        out("")
        out("=== WAVE_MAPPER ===")
        st = probe(w, WAVE_MAPPER, secs)
        if st.get("open"):
            out("  MAPPER  rms=%.5f dbFS=%6.1f peak=%6d nz=%d/%d blocks=%d"
                % (st["rms"], st["db"], st["peak"], st["nz"],
                   st["total"], st["blocks"]))
        else:
            out("  MAPPER OPEN FAILED rc=%s" % st.get("rc"))

    out("")
    out("=== verdict ===")
    alive = [(i, n, s) for (i, n, s) in results if s and s["peak"] > 0]
    if alive:
        best = max(alive, key=lambda r: r[2]["rms"])
        check("some wave-in device delivers samples", True,
              "device %d %s rms=%.5f" % (best[0], best[1], best[2]["rms"]))
        if len(alive) > 1:
            check("device selection matters", True,
                  "%d of %d devices carry signal" % (len(alive), len(results)))
    else:
        check("some wave-in device delivers samples", False,
              "every device returned exact zeros")
    # 现代 Windows 不再往 Sound Mapper 写默认录音设备（这台机器上就没有），
    # 所以这一条只作记录，不算失败。
    out("(info) registry default capture name: %s" % (defname or "(not stored)"))
    if do_matrix:
        check("some format delivers samples", bool(matrix_alive),
              ("%d working format(s), best=%s" % (len(matrix_alive),
               matrix_alive[0][2:5] if matrix_alive else "none")))

    out("")
    out("%d checks, %d FAIL" % (len(OK) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
