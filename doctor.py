# -*- coding: utf-8 -*-
"""Siri-OS 一键自检：依赖 / 配置 / 渲染 / 音频设备 / 麦克风 / 模型 / 常驻进程。

用法：
    python tools\siri_orb\doctor.py            # 全量检查（含 3 秒麦克风试音）
    python tools\siri_orb\doctor.py --no-mic   # 跳过麦克风（不想占用录音设备时用）

输出刻意全用 ASCII：本机控制台是 GBK，中文会花屏。
完整报告同时写到 tools\siri_orb\logs\doctor.txt。
"""
import io
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

OK = "[ OK ]"
WARN = "[WARN]"
FAIL = "[FAIL]"
INFO = "[INFO]"

_log = []
_nfail = 0
_nwarn = 0


def say(tag, msg):
    global _nfail, _nwarn
    line = "%s %s" % (tag, msg)
    try:
        print(line)
    except Exception:
        print(line.encode("ascii", "ignore").decode("ascii"))
    _log.append(line)
    if tag == FAIL:
        _nfail += 1
    elif tag == WARN:
        _nwarn += 1


def load_cfg():
    p = os.path.join(HERE, "config.json")
    with io.open(p, "r", encoding="utf-8") as f:
        return json.load(f)


import json  # noqa: E402  (放在 load_cfg 之后只是为了让上面的顺序读起来顺)


# ---------------------------------------------------------------- 1 依赖
def check_deps():
    say(INFO, "--- 1/10 python deps ---")
    missing = []
    for label, mod in (("numpy", "numpy"), ("PIL", "PIL"), ("requests", "requests"),
                        ("PyQt5", "PyQt5"), ("win32com", "win32com.client"),
                        ("cv2", "cv2")):
        try:
            __import__(mod)
            say(OK, "%s import ok" % label)
        except Exception as e:
            missing.append(label)
            say(FAIL, "%s import failed: %s" % (label, e))
    return missing


# ---------------------------------------------------------------- 2 配置
def check_config(cfg):
    say(INFO, "--- 2/10 config.json ---")
    sc = cfg.get("xiaomi_scan_code")
    if isinstance(sc, int) and 0 < sc < 256:
        say(OK, "xiaomi_scan_code = 0x%02X (sc bound)" % sc)
    else:
        say(FAIL, "xiaomi_scan_code not bound: %r" % (sc,))
    vk = cfg.get("xiaomi_vk")
    say(OK if isinstance(vk, int) else WARN, "xiaomi_vk = %r" % (vk,))

    go = cfg.get("glow_origin")
    ok = (isinstance(go, (list, tuple)) and len(go) == 2
          and all(isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0 for v in go))
    if ok:
        say(OK, "glow_origin = [%.4f, %.4f] (wave source / button)" % (float(go[0]), float(go[1])))
    else:
        say(FAIL, "glow_origin invalid: %r" % (go,))

    cp = cfg.get("caption_position", "top")
    say(OK if cp == "top" else WARN, "caption_position = %s" % cp)

    for k in ("wave_delay_sec", "wave_sweep_sec", "wave_band_frac"):
        v = cfg.get(k)
        if isinstance(v, (int, float)) and v > 0:
            say(OK, "%s = %s" % (k, v))
        else:
            say(FAIL, "%s invalid: %r" % (k, v))

    say(OK if cfg.get("show_orb") is False else WARN,
        "show_orb = %r (reference video has no centre orb)" % cfg.get("show_orb"))
    sp = cfg.get("system_prompt") or ""
    say(OK if len(sp) >= 10 else WARN, "system_prompt length = %d chars" % len(sp))
    return cfg


# ---------------------------------------------------------------- 3 渲染
def check_render(cfg, make_sheet=True):
    say(INFO, "--- 3/10 renderer self-test ---")
    try:
        import numpy as np
        from siri_orb import GlowRenderer, wave_radius
    except Exception as e:
        say(FAIL, "cannot import renderer: %s" % e)
        return

    W, H = 1440, 900
    go = cfg.get("glow_origin") or [0.9236, 0.9711]
    origin = (float(go[0]) * W, float(go[1]) * H)
    gr = GlowRenderer(W, H, origin=origin)
    bw = max(34.0, W * float(cfg.get("wave_band_frac", 0.042)))
    delay = float(cfg.get("wave_delay_sec", 0.2))
    sweep = float(cfg.get("wave_sweep_sec", 0.5))
    say(OK, "renderer %dx%d origin=(%.1f, %.1f) max_r=%.0f band=%.0f"
        % (W, H, origin[0], origin[1], gr.max_r, bw))

    # (a) 延迟期：整圈必须还是暗的（只有按钮亮）
    a = gr.render(0.0, 1.0, "listening", wave={"radius": -1.0e9, "width": bw})[..., 3]
    lit = int((a > 0.06).sum())
    if lit == 0:
        say(OK, "delay phase: whole ring dark (lit_px=0)")
    else:
        say(FAIL, "delay phase not dark (lit_px=%d)" % lit)

    # (b) 光带跟随波前：用「相邻两帧的增量」定位波前。
    #     注意不能直接对 alpha 取 argmax：稳态底光叠加后会饱和到 1.0，
    #     argmax 只会取到第一个饱和环，看起来像「波前不前进」（这个坑踩过一次）。
    #     增量法会把常驻底光抵消掉，剩下的就是这一帧新点亮的部分。
    db = gr.dbtn
    prev_img = gr.render(1.0, 1.0, "listening", wave={"radius": -1.0e9, "width": bw})[..., 3]
    prev_front = -1.0
    prev_lit = -1
    for R in (160.0, 420.0, 760.0, 1150.0):
        a = gr.render(1.0, 1.0, "listening", wave={"radius": R, "width": bw})[..., 3]
        gain = np.clip(a - prev_img, 0.0, None)
        gmax = float(gain.max())
        if gmax > 0.05:
            sel = gain > 0.6 * gmax
            front = float(db[sel].mean())
        else:
            front = -1.0
        lit = int((a > 0.06).sum())
        err = front - R
        tag = OK if abs(err) <= 90 else FAIL
        say(tag, "wave R=%6.0f -> front at %6.0f (err %+5.0f) lit_px=%d" % (R, front, err, lit))
        if prev_front >= 0 and front <= prev_front:
            say(FAIL, "front did not advance: %.0f -> %.0f" % (prev_front, front))
        if prev_lit >= 0 and lit < prev_lit:
            say(WARN, "lit area shrank from %d to %d" % (prev_lit, lit))
        prev_img, prev_front, prev_lit = a, front, lit

    # (c) 时间轴：真实时间 -> 波前半径
    say(OK, "timeline: press -> +%.2fs delay -> sweep %.2fs" % (delay, sweep))
    for tau in (0.0, 0.25, 0.5, 0.75, 1.0):
        wt = tau * sweep
        r = gr.max_r * wave_radius(tau)
        say(OK, "  t=+%.3fs (tau=%.2f) radius=%6.0f px" % (delay + wt, tau, r))

    # (d) 对照图
    if make_sheet:
        try:
            out = build_sheet(gr, bw, delay, sweep)
            say(OK, "wrote %s" % os.path.relpath(out, HERE))
        except Exception as e:
            say(WARN, "sheet build failed: %s" % e)


def build_sheet(gr, bw, delay, sweep):
    import numpy as np
    from PIL import Image

    cols, rows = 4, 2
    ts = [0.0, 0.10, 0.20, 0.28, 0.36, 0.44, 0.52, 0.62]
    tw, th = gr.lw, gr.lh
    # 背景：模拟一块桌面（深色 + 轻微渐变），做 alpha 合成的底
    yy = np.linspace(0.0, 1.0, th, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, tw, dtype=np.float32)[None, :]
    bg = np.zeros((th, tw, 3), dtype=np.float32)
    bg[..., 0] = 0.10 + 0.05 * yy
    bg[..., 1] = 0.11 + 0.05 * yy
    bg[..., 2] = 0.16 + 0.07 * (1.0 - xx)
    sheet = Image.new("RGB", (tw * cols, th * rows), (26, 28, 38))
    for i, t in enumerate(ts):
        if t < delay:
            wave = {"radius": -1.0e9, "width": bw}
        elif t < delay + sweep:
            tau = (t - delay) / max(1e-3, sweep)
            wave = {"radius": gr.max_r * wave_radius(tau), "width": bw}
        else:
            wave = None
        img = gr.render(t, 1.0, "listening", wave)
        rgb = np.clip(img[..., :3], 0, 1)
        al = np.clip(img[..., 3:4], 0, 1)
        comp = bg * (1.0 - al) + rgb * al          # 必须按 alpha 合成
        pil = Image.fromarray((comp * 255).astype(np.uint8), "RGB")
        sheet.paste(pil, ((i % cols) * tw, (i // cols) * th))
    out = os.path.join(HERE, "doctor_wave.png")
    sheet.save(out)
    return out


# ---------------------------------------------------------------- 4 音频设备
def check_audio_devices():
    say(INFO, "--- 4/10 audio devices (SAPI) ---")
    out = os.path.join(HERE, "tmp", "audio_probe.txt")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
    except Exception:
        pass
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           os.path.join(HERE, "probe_audio.ps1"), "-OutFile", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        say(FAIL, "probe_audio.ps1 failed: %s" % e)
        return
    try:
        with io.open(out, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    except Exception as e:
        say(FAIL, "cannot read audio probe output: %s" % e)
        return
    recs = [l for l in lines if l.startswith("REC|")]
    zh = [l for l in recs if "|zh-CN|" in l]
    for l in recs:
        say(OK, "recognizer %s" % l.split("|", 1)[1].replace("|", " / "))
    if not recs:
        say(FAIL, "no SAPI recognizer found")
    elif zh:
        say(OK, "zh-CN recognizer present (offline dictation usable)")
    else:
        say(FAIL, "no zh-CN recognizer installed")
    voices = [l for l in lines if l.startswith("VOICE|")]
    hu = [v for v in voices if "huihui" in v.lower()]
    if hu:
        say(OK, "zh TTS voice: %s" % hu[0].split("|", 1)[1])
    else:
        say(WARN, "no 'Huihui' voice; available: %s" % ", ".join(v.split("|", 1)[1] for v in voices[:6]))
    for l in lines:
        if l.startswith("REC_ERR") or l.startswith("VOICE_ERR"):
            say(WARN, l)


# ---------------------------------------------------------------- 5 麦克风
def check_mic():
    say(INFO, "--- 5/10 microphone capture (3s) ---")
    out = os.path.join(HERE, "tmp", "doctor_stt.txt")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           os.path.join(HERE, "listen.ps1"), "-OutFile", out, "-TimeoutSec", "3", "-Language", "zh-CN"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = r.stdout.decode("utf-8", "ignore").strip()
    except Exception as e:
        say(FAIL, "listen.ps1 failed: %s" % e)
        return
    if txt.startswith("OK"):
        say(OK, "mic opened + recognizer ran: %s" % txt)
        try:
            with io.open(out, "r", encoding="utf-8") as f:
                heard = f.read().strip()
        except Exception:
            heard = ""
        if heard:
            say(OK, "captured %d chars of speech (recognition works)" % len(heard))
        else:
            say(WARN, "no speech captured in 3s (engine ok, accuracy untested)")
    else:
        say(FAIL, "listen.ps1 said: %s" % (txt or "(no output)"))


# ---------------------------------------------------------------- 6 模型
def check_llm(cfg):
    say(INFO, "--- 6/10 LLM backends ---")
    lcfg = cfg.get("llm") or {}
    try:
        from llm import MinimaxBackend, DeepSeekBackend, ZhipuBackend
    except Exception as e:
        say(FAIL, "cannot import llm: %s" % e)
        return
    mm = MinimaxBackend(model=lcfg.get("minimax_model"),
                        api_key=lcfg.get("minimax_api_key"),
                        base_url=lcfg.get("minimax_base_url"))
    ds = DeepSeekBackend(model=lcfg.get("deepseek_model"),
                          api_key=lcfg.get("deepseek_api_key"),
                          base_url=lcfg.get("deepseek_base_url"))
    zh = ZhipuBackend(model=lcfg.get("zhipu_model"),
                      api_key=lcfg.get("zhipu_api_key"),
                      base_url=lcfg.get("zhipu_base_url"))
    for tag, name, b in (("minimax", "minimax", mm), ("deepseek", "deepseek", ds),
                         ("zhipu", "zhipu", zh)):
        key = b.api_key or ""
        if not key:
            say(FAIL, "%s: no api key -> run: python set_key.py %s" % (name, name))
            continue
        if len(key) < 16:
            say(FAIL, "%s: key looks like a placeholder (%d chars, local gateway creds are unusable)" % (name, len(key)))
            continue
        try:
            # 后端 chat() 没有 timeout 关键字，别传（会 TypeError 误报成"调用失败"）
            out = b.chat([{"role": "user", "content": "say hi in one short Chinese sentence"}],
                         max_tokens=64)
            say(OK, "%s: live call OK -> %s" % (name, out.replace("\n", " ")[:80]))
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            # 余额不足是「账户状态」，不是「接线坏了」—— 报 WARN 更准
            if "402" in msg or "insufficient_balance" in low or "insufficient balance" in low:
                say(WARN, "%s: key OK but account has no balance (402) -> top up, then restart assistant" % name)
            elif "401" in msg or "403" in msg or "invalid api key" in low:
                say(FAIL, "%s: key rejected by server (401/403) -> replace the key" % name)
            else:
                say(FAIL, "%s: live call failed -> %s" % (name, msg))


# ---------------------------------------------------------------- 7 常驻
def check_service():
    say(INFO, "--- 7/10 resident service ---")
    out = os.path.join(HERE, "tmp", "proc_probe.txt")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
    except Exception:
        pass
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           os.path.join(HERE, "probe_proc.ps1"), "-OutFile", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with io.open(out, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    except Exception as e:
        say(WARN, "process probe failed: %s" % e)
        return
    hit = None
    for l in lines:
        if not l.startswith("PROC|"):
            continue
        _, pid, cmdline = l.split("|", 2)
        low = cmdline.lower()
        # 源码跑 = siri_orb.py；装好的打包版 = Siri-OS.exe —— 两种都算「常驻在跑」。
        # 只认 siri_orb.py 的话，跑了 exe 的机器会被误报成「服务没启动」。
        # ⚠️ CIM 给的 cmdline **带引号**（"<安装目录>\Siri-OS.exe"），
        #    所以要 strip 掉引号再比，否则永远匹配不上。
        bare = low.strip('"')
        if ("siri_orb.py" in cmdline and "--demo" not in cmdline) \
                or bare.endswith("siri-os.exe"):
            hit = (pid, cmdline)
            break
    if hit:
        say(OK, "siri_orb service running: pid=%s" % hit[0])
        # 打出跑的是哪一份 —— 「配置被重置」这类问题第一眼就要看这个
        say(INFO, "  cmdline: %s" % hit[1][:160])
    else:
        say(WARN, "siri_orb service NOT running "
                  "(start the installed Siri-OS.exe, or: python tools\\siri_orb\\siri_orb.py)")


# ---------------------------------------------------------------- 8 开机自启
def check_autostart(cfg):
    say(INFO, "--- 8/10 autostart + config path ---")
    try:
        import install_autostart as ia
    except Exception as e:
        say(WARN, "install_autostart unavailable: %r" % e)
        return
    p = ia.shortcut_path()
    if not os.path.exists(p):
        say(WARN, "autostart shortcut MISSING: %s" % p)
        say(INFO, "  (enable it in the settings panel of the packaged exe)")
        return
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        lnk = shell.CreateShortCut(p)
        target = lnk.Targetpath or ""
        args = lnk.Arguments or ""
        wdir = lnk.WorkingDirectory or ""
    except Exception as e:
        say(WARN, "cannot read shortcut: %r" % e)
        return
    say(INFO, "  target : %s" % target)
    say(INFO, "  args   : %s" % args)
    say(INFO, "  wdir   : %s" % wdir)
    if ia.FROZEN:
        if target and os.path.normcase(target) == os.path.normcase(ia.EXE or ""):
            say(OK, "autostart points at this exe")
        else:
            say(FAIL, "autostart does NOT point at this exe -> %s" % target)
    else:
        if ia.is_temp_workspace(target) or ia.is_temp_workspace(args):
            say(FAIL, "autostart points into a TEMP session dir "
                      "(reboot starts another build -> config looks reset)")
        else:
            say(OK, "autostart target is outside any session dir: %s" % target)
    try:
        import cfgstore
        say(INFO, "  config : %s" % cfgstore.CONFIG_PATH)
        if not ia.FROZEN:
            say(INFO, "  (this build reads the config above; "
                      "the installed exe reads the one next to it)")
    except Exception:
        pass


# ---------------------------------------------------------------- 9 位置
def check_location(cfg):
    say(INFO, "--- 9/10 location ---")
    try:
        import locate as locate_mod
    except Exception as e:
        say(WARN, "locate module unavailable: %r" % e)
        return
    if not locate_mod.enabled(cfg):
        say(INFO, "location is OFF (ctx.location) - nothing to check")
        return
    t0 = time.time()
    p = locate_mod.refresh(cfg, force=True)
    d = locate_mod.describe()
    if p:
        say(OK, "located: %s  (source=%s, %.1fs)" % (
            p.get("text"), p.get("source"), time.time() - t0))
        say(INFO, "  gps ok=%s  accuracy=%s m" % (d.get("gps_ok"), d.get("gps_acc_m")))
        say(INFO, "  (only the district text ever reaches the model)")
    else:
        say(WARN, "could not locate (last error: %s)" % (d.get("last_err") or "?"))
        say(INFO, "  check the Windows location service and the network")


# ---------------------------------------------------------------- 10 天气
def check_weather(cfg):
    say(INFO, "--- 10/10 weather ---")
    try:
        import weather as weather_mod
    except Exception as e:
        say(WARN, "weather module unavailable: %r" % e)
        return
    t0 = time.time()
    place = ""
    try:
        import locate as locate_mod
        place = locate_mod.text(cfg) or ""
    except Exception:
        pass
    if not place:
        say(INFO, "no place cached - checking Shanghai instead")
        place = "上海市"
    p = weather_mod.fetch(place, cfg)
    if not p:
        say(WARN, "weather lookup failed: %s"
            % (weather_mod.last_error() or "unknown"))
        return
    say(OK, "weather: %s  (%.1fs)" % (weather_mod.summary(p), time.time() - t0))
    say(INFO, "  icon=%s code=%s hi=%s lo=%s pop=%s"
        % (p.get("icon"), p.get("code"), p.get("hi"), p.get("lo"), p.get("pop")))
    say(INFO, "  (the district NAME is geocoded; the user's GPS coords are never sent)")


def main():
    t0 = time.time()
    say(INFO, "Siri-OS doctor @ %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    missing = check_deps()
    if missing:
        say(FAIL, "missing deps: %s" % ", ".join(missing))
        return finish(t0)
    cfg = check_config(load_cfg())
    check_render(cfg)
    check_audio_devices()
    if "--no-mic" in sys.argv:
        say(WARN, "mic test skipped (--no-mic)")
    else:
        check_mic()
    check_llm(cfg)
    check_service()
    check_autostart(cfg)
    check_location(cfg)
    check_weather(cfg)
    return finish(t0)


def finish(t0):
    say(INFO, "--- summary ---")
    say(INFO if _nfail == 0 else FAIL, "%d FAIL / %d WARN / %.1fs" % (_nfail, _nwarn, time.time() - t0))
    try:
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        with io.open(os.path.join(HERE, "logs", "doctor.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(_log))
    except Exception:
        pass
    return 1 if _nfail else 0


if __name__ == "__main__":
    sys.exit(main())
