# -*- coding: utf-8 -*-
"""端到端实测：录声时边光随电平向内收缩（实机、真实常驻程序）。

流程：
  1. config 的 level_source 临时切到 "sim"（常驻程序会热重载），
     sim_rate 调成 3.0 让音节包络在抓帧速率下可分辨
  2. 注入小米按键扫描码 -> 常驻程序进入 listening 态
  3. 连拍实机画面**右侧边缘**竖条（避开顶部字幕卡和右下角按钮）
  4. 逐帧沿边缘法线方向拟合弥散长度 L -> L 就是「往里钻」的深度
  5. 还原 config，打印 L 序列 + 拼图

⚠️ 本机 ImageGrab 返回的是**物理像素 2880x1800**（不是逻辑 1440x900），
   量出来的深度也是物理像素，所以约为渲染器逻辑深度的 2 倍。

用法： python tools\siri_orb\level_live.py [秒数]
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import json
import os
import shutil
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageGrab

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import inject_scan
import level_check

CFG = os.path.join(HERE, "config.json")
SCREEN_W, SCREEN_H = 2880, 1800          # 物理像素
STRIP_W, STRIP_H = 600, 660
BBOX = (SCREEN_W - STRIP_W, 500, SCREEN_W, 500 + STRIP_H)
FIT_LO_PX, FIT_HI_PX = 60, 300           # 拟合窗口（物理像素）


def patch_config(level_source="sim", sim_rate=3.0):
    """改 config 并返回原始字节（用于还原）。"""
    if not os.path.exists(CFG):
        import cfgstore
        cfgstore.save(cfgstore.DEFAULTS)      # 第一次跑：先落一份默认配置
    with open(CFG, "rb") as f:
        raw = f.read()
    cfg = json.loads(raw.decode("utf-8"))
    cfg["level_source"] = level_source
    cfg["sim_rate"] = sim_rate
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return raw


def grab_profile():
    """抓右侧边缘竖条，返回沿法线向内的亮度剖面（物理像素 -> 亮度）。"""
    im = ImageGrab.grab(bbox=BBOX).convert("L")
    a = np.asarray(im, dtype=np.float64)
    prof = np.median(a[:, ::-1], axis=0)   # 反转列 -> prof[0] 是最贴边的一列
    return prof, im


def grab_background(n=8):
    """空闲态抓几张，取中位数当背景剖面。

    实机测量不能直接拟合原始亮度：边光是**叠在窗口内容上**的，
    底层亮度会污染指数拟合。减掉背景后剩下的才是边光自己的贡献。
    """
    ps = []
    for _ in range(n):
        prof, _im = grab_profile()
        ps.append(prof)
    return np.median(np.array(ps), axis=0)


def main():
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 9.0
    raw = patch_config()
    frames = []
    try:
        print("config -> level_source=sim, sim_rate=3.0 ; waiting hot reload ...")
        time.sleep(1.5)
        print("grabbing idle background ...", flush=True)
        bg = grab_background()
        inject_scan.send_scan(0x72)
        t_inj = time.time()
        print("injected scan 0x72", flush=True)
        while time.time() - t_inj < dur:
            prof, im = grab_profile()
            frames.append((time.time() - t_inj, prof, im))
    finally:
        with open(CFG, "wb") as f:
            f.write(raw)
        print("config restored")

    if len(frames) < 8:
        print("[FAIL] too few frames: %d" % len(frames))
        return 1
    fps = (len(frames) - 1) / max(1e-6, frames[-1][0] - frames[0][0])
    print("captured %d frames  (%.1f fps)" % (len(frames), fps))

    level_check.FIT_LO = FIT_LO_PX
    level_check.FIT_HI = FIT_HI_PX

    print("")
    print("  t(s)   L(px)   rimEdge   A        rim?")
    series = []
    for fi, (t, prof, im) in enumerate(frames):
        rim = np.clip(prof - bg, 0, None)          # 减掉背景，只留边光自己的贡献
        span = float(rim.max() - rim.min())
        f = level_check.fit_length(rim, scale=1.0)
        L = f[0] if f else 0.0
        A = f[1] if f else 0.0
        has_rim = span > 1.5 and A > 0.0
        series.append((t, L, float(rim[0]), span, has_rim, fi))
        print("%6.2f  %6.1f  %8.1f  %8.3f  %s" % (t, L, rim[0], A, "yes" if has_rim else "no"))

    live = [s for s in series if s[4]]
    if len(live) < 5:
        print("[FAIL] rim not visible in enough frames (%d)" % len(live))
        return 1
    Ls = np.array([s[1] for s in live])
    edges = np.array([s[2] for s in live])
    print("")
    print("rim frames: %d / %d" % (len(live), len(series)))
    print("L: min=%.0f  max=%.0f  span=%.0f  (physical px)" % (Ls.min(), Ls.max(), Ls.max() - Ls.min()))
    print("expected: level 0 -> ~79px (2x39.5 logical), level 1 -> ~154px")
    corr = float(np.corrcoef(Ls, edges)[0, 1]) if Ls.std() > 1e-6 and edges.std() > 1e-6 else 0.0
    print("corr(L, edgeLum) = %.3f" % corr)
    # 实机判据（离线渲染器那侧另有 level_check.py 的严格判据）：
    #   1) 边光在大部分帧里都在
    #   2) 边光亮度随音节包络明显起伏
    #   3) 钻入深度随之变化
    ok = (len(live) >= 0.6 * len(series)
          and (edges.max() - edges.min()) >= 15.0
          and (Ls.max() - Ls.min()) >= 15.0
          and corr >= 0.5)
    print("[%s] rim depth tracks the level envelope" % ("PASS" if ok else "FAIL"))

    order = np.argsort(Ls)
    picks = [order[0], order[len(order) // 2], order[-1]]
    # 只裁屏幕右缘一块，把三档（最浅/居中/最深）并排摆开，直观看到「往里钻」
    CW, CH = 340, 340
    up = 2
    cell_w, cell_h = CW * up, CH * up
    sheet = Image.new("RGB", (cell_w * len(picks), cell_h + 30), (18, 20, 26))
    dr = ImageDraw.Draw(sheet)
    for j, idx in enumerate(picks):
        t, L, edge, span, _, fi = live[idx]
        full = frames[fi][2].convert("RGB")
        crop = full.crop((full.width - CW, 560, full.width, 560 + CH)).resize((cell_w, cell_h), Image.LANCZOS)
        x = j * cell_w
        sheet.paste(crop, (x, 30))
        dr.text((x + 8, 9), "L=%.0fpx  edge=%.0f  t=%.2fs" % (L, edge, t), fill=(225, 225, 235))
        dr.rectangle([x, 30, x + cell_w - 1, 30 + cell_h - 1], outline=(60, 62, 74))
    out = os.path.join(HERE, "level_live_sheet.png")
    sheet.save(out)
    print("wrote %s" % out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
