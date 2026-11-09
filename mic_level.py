# -*- coding: utf-8 -*-
"""麦克风实时电平（winmm waveIn 直采），给「边光随录音向内收缩」驱动用。

只依赖标准库 ctypes，不需要 sounddevice / pyaudio。

用法：
    python tools\siri_orb\mic_level.py 5              # 采 5 秒，打印电平
    python tools\siri_orb\mic_level.py 5 --play       # 同时从扬声器放声音（自检麦克风）
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


def _api():
    w = ctypes.WinDLL("winmm.dll")
    w.waveInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), wt.DWORD,
                             ctypes.POINTER(WAVEFORMATEX), wt.DWORD,
                             wt.DWORD, wt.DWORD]
    w.waveInOpen.restype = ctypes.c_uint   # MMRESULT
    # ⚠️ 每个 API 的参数个数不一样，别抄成一样：
    #   waveInStart/Reset/Stop/Close 只收 hWaveIn 一个参数
    for fn in ("waveInStart", "waveInReset", "waveInStop", "waveInClose"):
        getattr(w, fn).argtypes = [ctypes.c_void_p]
        getattr(w, fn).restype = ctypes.c_uint   # MMRESULT 没有 ctypes.wintypes 版本
    for fn in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
        getattr(w, fn).argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
        getattr(w, fn).restype = ctypes.c_uint
    return w


class MicMeter(object):
    """按块（默认 50ms）返回 0..1 的电平，以及 dBFS。"""

    def __init__(self, block_ms=BLOCK_MS):
        self.block_ms = block_ms
        self.w = _api()
        self.hIn = ctypes.c_void_p()
        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, 1, SAMPLE_RATE,
                            SAMPLE_RATE * 2, 2, 16, 0)
        rc = self.w.waveInOpen(ctypes.byref(self.hIn), WAVE_MAPPER,
                               ctypes.byref(fmt), 0, 0, CALLBACK_NULL)
        if rc != 0:
            raise RuntimeError("waveInOpen failed rc=%d" % rc)
        self.nbytes = SAMPLE_RATE * 2 * block_ms // 1000
        self.buf = (ctypes.c_ubyte * self.nbytes)()
        self.hdr = WAVEHDR()
        self.hdr.lpData = ctypes.cast(self.buf, ctypes.POINTER(ctypes.c_ubyte))
        self.hdr.dwBufferLength = self.nbytes
        self.open = True

    def _prepare(self):
        self.hdr.dwFlags = 0
        self.hdr.dwBytesRecorded = 0
        rc = self.w.waveInPrepareHeader(self.hIn, ctypes.byref(self.hdr),
                                        ctypes.sizeof(self.hdr))
        if rc != 0:
            raise RuntimeError("waveInPrepareHeader rc=%d" % rc)

    def _unprepare(self):
        self.w.waveInUnprepareHeader(self.hIn, ctypes.byref(self.hdr),
                                     ctypes.sizeof(self.hdr))

    def block(self):
        """采一块，返回 (rms, dbfs, peak)。"""
        self._prepare()
        # ⚠️⚠️ **waveInAddBuffer 不能漏**（2026-11-08 实测的真 bug）。
        #    漏了它，缓冲区就**从没进过设备的输入队列** —— 驱动压根不会去填它，
        #    WHDR_DONE 永远不置位，我们每次只能等超时，然后读到一整块零。
        #    症状特别像「麦克风坏了」：
        #      - 每块耗时都是超时上限（50ms 的块要走 0.55s），
        #      - rms / peak 全是**精确的 0**（真麦克风在安静房间也有底噪，不会是整 0），
        #      - 扬声器放测试音也照样全 0。
        #    加上这一行以后：每块 0.05s 完成，rms 正常、peak 正常。
        rc = self.w.waveInAddBuffer(self.hIn, ctypes.byref(self.hdr),
                                  ctypes.sizeof(self.hdr))
        if rc != 0:
            self._unprepare()
            raise RuntimeError("waveInAddBuffer rc=%d" % rc)
        rc = self.w.waveInStart(self.hIn)
        if rc != 0:
            self._unprepare()
            raise RuntimeError("waveInStart rc=%d" % rc)
        deadline = time.time() + self.block_ms / 1000.0 + 0.5
        while not (self.hdr.dwFlags & WHDR_DONE):
            if time.time() > deadline:
                break
            time.sleep(0.003)
        self.w.waveInStop(self.hIn)
        # dwBytesRecorded 要在 reset **之前**读 —— waveInReset 会把它清零
        n = self.hdr.dwBytesRecorded or self.nbytes
        self.w.waveInReset(self.hIn)
        self._unprepare()
        data = bytes(bytearray(self.buf[:n]))
        count = len(data) // 2
        if count == 0:
            return 0.0, -99.0, 0
        s = array.array("h")
        s.frombytes(data[:count * 2])
        peak = 0
        acc = 0
        nz = 0
        for v in s:
            a = v if v >= 0 else -v
            if a > peak:
                peak = a
            acc += v * v
            if v:
                nz += 1
        rms = math.sqrt(float(acc) / count) / 32768.0
        db = 20.0 * math.log10(rms) if rms > 1e-7 else -99.0
        return rms, db, peak

    def close(self):
        if getattr(self, "open", False):
            try:
                self.w.waveInClose(self.hIn)
            except Exception:
                pass
            self.open = False


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    play = "--play" in sys.argv
    if play:
        import threading
        import winsound
        import threading as _th
        cands = [r"C:\Windows\Media\Alarm01.wav", r"C:\Windows\Media\WindowsNotify.wav"]
        snd = next((c for c in cands if os.path.exists(c)), None)

        def go():
            if not snd:
                return
            for _ in range(int(secs / 1.2) + 1):
                winsound.PlaySound(snd, winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                time.sleep(1.2)
        threading.Thread(target=go, daemon=True).start()
    m = MicMeter()
    print("mic open ok, block=%dms" % m.block_ms)
    t0 = time.time()
    rows = []
    while time.time() - t0 < secs:
        rms, db, peak = m.block()
        rows.append((time.time() - t0, rms, db, peak))
    m.close()
    print("%6s %8s %8s %6s" % ("t", "rms", "dbFS", "peak"))
    for t, rms, db, peak in rows:
        print("%6.3f %8.5f %8.1f %6d" % (t, rms, db, peak))
    if rows:
        mx = max(r[1] for r in rows)
        print("max rms=%.5f  max dbFS=%.1f" % (mx, 20 * math.log10(mx) if mx > 1e-7 else -99))


if __name__ == "__main__":
    main()
