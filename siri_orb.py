# -*- coding: utf-8 -*-
"""Siri-OS —— 仿第一版 Apple Intelligence 的桌面语音助手（Windows）。

视觉：屏幕四周流动的虹彩光晕 + 底部发光 Siri 光球 + 悬浮字幕卡（画在一张透明、
      置顶、鼠标穿透的全屏层上）。
听觉：SAPI 离线中文听写(zh-CN) -> 本地 MiniMax 网关 / DeepSeek -> SAPI 中文播报。
按键：小米笔记本上闲置的「小米语音助手」键（扫描码在 config.json 绑定），
      另配一个兜底热键。

用法：
    python siri_orb.py            正常启动（托盘常驻，按键唤醒）
    python siri_orb.py --demo     视觉演示：自动循环 聆听->思考->说话
    python keyprobe.py            扫描码嗅探：找出小米语音键的扫描码
"""
import os
import sys
import json
import time
import ctypes
import traceback
import subprocess

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cfgstore
import i18n
from llm import LLM
import voice as voice_mod
from keyhook import KeyHook

CONFIG_PATH = cfgstore.CONFIG_PATH
LOG_DIR = os.path.join(HERE, "logs")
KEY_LOG = os.path.join(LOG_DIR, "keyhook.log")

# 默认值只有一份，在 cfgstore 里（设置面板的「恢复默认」也是用它）
DEFAULT_CONFIG = cfgstore.DEFAULTS

# 状态文字（聆听中/思考中/说话中 ↔ Listening/Thinking/Speaking）在 i18n 里，
# 这里不再硬编码 —— 界面语言一切换就跟着变。


def _merge(base, new):
    out = dict(base)
    for k, v in (new or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    except Exception as e:
        print("[siri_orb] config read failed: %s" % e)
    return cfg


def _hsv(h, s, v):
    """向量化 HSV->RGB，h 为 [0,1) 数组。"""
    h = np.mod(h, 1.0) * 6.0
    i = np.floor(h).astype(np.int32) % 6
    f = h - np.floor(h)
    # 标准 HSV->RGB 的三个中间量：
    #   p = v*(1-s)          全暗端
    #   q = v*(1-f*s)        下降沿
    #   t = v*(1-(1-f)*s)    上升沿
    # ⚠️ 之前这三个全写错了（p 直接置 0、q/t 互换了 f 的位置），
    #    导致蓝紫被渲成纯品红、橙被渲成橄榄黄 —— 颜色过渡怎么调都不自然。
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [v, q, p, p, t, v])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [t, v, v, q, p, p])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1)


def _blur(arr, radius):
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return arr
    a = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
    im = Image.fromarray(a)
    im = im.filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(im).astype(np.float32) / 255.0


def _pchip_slopes(xs, ys):
    """周期 PCHIP（Fritsch-Carlson）切线。

    为什么不用均匀 Catmull-Rom：色相锚点间距不等（有一段 0.25），
    均匀 CR 会在锚点处留下斜率断点 —— 看上去就是「颜色过渡不自然」的拐点。
    PCHIP 每个节点只有一个切线、相邻两段共用，天然 C1；
    局部极值处切线取 0，转折圆润且不会过冲。
    """
    n = len(xs)
    h = np.empty(n)
    h[:-1] = np.diff(xs)
    h[-1] = (xs[0] + 1.0) - xs[-1]          # 环绕区间
    d = np.empty(n)
    d[:-1] = (ys[1:] - ys[:-1]) / h[:-1]
    d[-1] = (ys[0] - ys[-1]) / h[-1]
    m = np.zeros(n)
    for i in range(n):
        ip = (i - 1) % n
        if d[ip] * d[i] <= 0.0:
            m[i] = 0.0
        else:
            w1 = 2.0 * h[i] + h[ip]
            w2 = h[i] + 2.0 * h[ip]
            m[i] = (w1 + w2) / (w1 / d[ip] + w2 / d[i])
    return m


def _pchip_eval(xe, ye, me, theta):
    """周期 PCHIP 求值。xe/ye/me 为扩展后的节点/值/切线（长度 n+1）。"""
    n = len(xe) - 1
    he = np.diff(xe)
    idx = np.clip(np.searchsorted(xe, theta, side="right") - 1, 0, n - 1)
    hh = he[idx]
    t = (theta - xe[idx]) / hh
    t2 = t * t
    t3 = t2 * t
    h00 = 2.0 * t3 - 3.0 * t2 + 1.0
    h10 = t3 - 2.0 * t2 + t
    h01 = -2.0 * t3 + 3.0 * t2
    h11 = t3 - t2
    return (h00 * ye[idx] + h10 * hh * me[idx]
            + h01 * ye[idx + 1] + h11 * hh * me[idx + 1])


def _to_qimage(arr):
    a = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
    a = np.ascontiguousarray(a)
    h, w = a.shape[:2]
    img = QtGui.QImage(a.data, w, h, w * 4, QtGui.QImage.Format_RGBA8888)
    return img.copy()


class GlowRenderer(object):
    """屏幕四周的弥散虹彩边光（第一版 Apple Intelligence）。

    参考特征：
      * 没有硬边线，是从屏幕边缘往里弥散的柔光；
      * 四个角上各有一团更亮的热斑；
      * 色相沿边缘平滑过渡：左=蓝青、上=紫、右=粉、下=橙、左下=品红；
      * 屏幕中心保持干净，只有极淡的色调浸染。
    """

    # (theta, 色相, 饱和度) —— 全部来自参考视频实测
    #   tools/siri_orb/ref_measure.py，t=11s 那帧（扩散刚结束、配色最干净）
    # theta：椭圆参数化角，0=右，顺时针一圈（0.25=下，0.5=左，0.75=上）
    #
    # 参考视频的颜色结构是「三个色池 + 低饱和过渡」，不是绕一圈的彩虹：
    #   右侧蓝紫(0.00-0.13) -> 底部到左侧一大片品红(0.20-0.53)
    #   -> 顶部大跨度低饱和过渡(0.53-0.80) -> 右上角一条橙(0.80-0.91)
    #   -> 右侧回到蓝紫(0.91-1.00)
    # 关键：过渡区饱和度掉到 0.26-0.44，色池中心 0.84-0.90。
    # 之前饱和度恒定 0.82，每次过渡都是满饱和彩虹横扫 —— 这就是「过渡不自然」的根因。
    PALETTE = (
        (0.000, 0.670, 0.48),
        (0.135, 0.700, 0.52),
        (0.200, 0.860, 0.60),
        (0.270, 0.922, 0.80),
        (0.360, 0.944, 0.90),
        (0.460, 0.938, 0.88),
        (0.530, 0.915, 0.70),
        (0.600, 0.810, 0.44),
        (0.660, 0.900, 0.33),
        (0.720, 0.965, 0.40),
        (0.780, 1.025, 0.54),
        (0.850, 1.075, 0.84),
        (0.910, 1.085, 0.70),
        (0.960, 0.870, 0.26),
    )
    # 色相/饱和度各走一条周期 PCHIP 样条（见 _pchip_slopes）

    def __init__(self, w, h, scale=3, origin=None, marquee_period=6.0, marquee_trail=0.09):
        self.w, self.h = w, h
        self.marquee_period = max(0.5, float(marquee_period))   # 跑马灯绕一圈几秒
        self.marquee_trail = max(0.02, float(marquee_trail))     # 尾巴长度（圈的比例）
        self.lw = max(24, w // scale)
        self.lh = max(24, h // scale)
        yy, xx = np.mgrid[0:self.lh, 0:self.lw].astype(np.float32)
        nx = (xx - (self.lw - 1) / 2.0) / max(1.0, (self.lw - 1) / 2.0)
        ny = (yy - (self.lh - 1) / 2.0) / max(1.0, (self.lh - 1) / 2.0)
        self.theta = np.mod(np.arctan2(ny, nx) / (2.0 * np.pi), 1.0)
        edge = np.minimum(np.minimum(xx, self.lw - 1 - xx),
                          np.minimum(yy, self.lh - 1 - yy))
        self.dist = edge * scale
        corners = ((0.0, 0.0), (self.lw - 1.0, 0.0),
                   (self.lw - 1.0, self.lh - 1.0), (0.0, self.lh - 1.0))
        dc = None
        for cx, cy in corners:
            d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) * scale
            dc = d if dc is None else np.minimum(dc, d)
        self.dcorner = dc

        # 按钮（波源）位置：默认放在任务栏右下角「电量与日期之间」那道缝里
        # （小米 AI 键在这台机器任务栏上的实际位置，按自己的键位改）。
        # 想看参考视频里 iPhone 侧边按键的原始位置，把 glow_origin 改成 [0.988, 0.25]。
        if origin is None:
            origin = (w * 0.9236, h * 0.9711)
        self.origin = (float(origin[0]), float(origin[1]))
        ox, oy = self.origin
        self.dbtn = np.sqrt((xx * scale - ox) ** 2 + (yy * scale - oy) ** 2)
        self.max_r = float(max(np.hypot(ox - 0.0, oy - 0.0),
                                np.hypot(ox - w, oy - 0.0),
                                np.hypot(ox - 0.0, oy - h),
                                np.hypot(ox - w, oy - h)))

        # 色相/饱和度样条：周期 PCHIP
        xs = np.array([p[0] for p in self.PALETTE], dtype=np.float64)
        hs = np.array([p[1] for p in self.PALETTE], dtype=np.float64)
        ss = np.array([p[2] for p in self.PALETTE], dtype=np.float64)
        self._ph = _pchip_slopes(xs, hs)
        self._ps = _pchip_slopes(xs, ss)
        self._xe = np.concatenate((xs, [xs[0] + 1.0]))
        self._ye_h = np.concatenate((hs, [hs[0]]))
        self._me_h = np.concatenate((self._ph, [self._ph[0]]))
        self._ye_s = np.concatenate((ss, [ss[0]]))
        self._me_s = np.concatenate((self._ps, [self._ps[0]]))

    def _palette(self, theta):
        """按 theta 返回 (色相, 饱和度)。"""
        h = _pchip_eval(self._xe, self._ye_h, self._me_h, theta)
        s = _pchip_eval(self._xe, self._ye_s, self._me_s, theta)
        return h, s

    def render(self, t, intensity, mode="listening", wave=None, marquee_gain=1.0,
                level=0.0):
        d = self.dist
        # 录声时的麦克风电平（0..1）驱动边光**向内收缩**：
        # 声音越大 -> 辉光往里钻得越深、整体越亮；声音越小 -> 收回贴边。
        # 参考视频实测：峰值时发光层钻到 15% 屏宽，收缩后回到 5-7%。
        lvl = max(0.0, min(1.0, float(level)))
        depth_scale = 1.0 + 0.95 * lvl
        bright = 0.85 + 0.55 * lvl
        # 调色板整体极慢漂移，配色身份（紫顶/粉右/橙底）要一直保持住
        speed = {"listening": 0.0015, "thinking": 0.0040, "speaking": 0.0022}.get(mode, 0.0015)
        hue, sat = self._palette(np.mod(self.theta + t * speed, 1.0))
        rgb = _hsv(hue, sat, 1.0)

        # u: 0=右下角, 0.125=下边, 0.25=左下, 0.375=左边, 0.5=左上角, ... 1=回到右下
        u = np.mod(self.theta - 0.125, 1.0)
        # ---- 角向跑马灯：一颗亮头 + 一条短尾，绕屏幕边缘追一圈 ----
        # 一圈几秒由 config 的 marquee_period_sec 决定（thinking 更快、speaking 略快）
        # 全程恒定速度：状态之间不再变速 —— 调速只有一个旋钮（marquee_period_sec），
        # 也不会出现「听写时慢、说话时突然快起来」的不一致感
        period = self.marquee_period
        phase = (t / period) % 1.0                             # 亮头位置（顺时针：右下→下→左→上→右）

        behind = np.mod(phase - u, 1.0)                         # 0=波头，越大越早被扫过
        dist = np.minimum(behind, 1.0 - behind)                 # 到波头的最近距离

        fall = np.exp(-np.maximum(d - 1.0, 0.0) / (50.0 * depth_scale))   # 边缘弥散（随音量变深）
        corner = np.exp(-self.dcorner / 130.0)                   # 四角热斑
        ctip = np.exp(-self.dcorner / 26.0) * 0.55               # 角上最亮的核心

        # 两层结构：
        #   base = 常驻底光（整圈一直有一层淡虹彩，扫完不会黑掉）
        #   wave_env = 行进波包络（只跟着波头走）
        # 单层 exp 衰减要么拖尾铺满一圈、要么远处直接黑掉，两层才兼顾得了。
        ring = fall * 0.50 * bright                            # 边缘弥散（随音量变亮）
        hot = (corner * 0.40 + ctip * 0.25) * bright            # 四角热斑
        base = (ring + hot) * 0.45                              # 常驻底光（保证扫完不黑）
        wave_env = ring * 0.75 + hot * 1.00                     # 行进波包络（更亮）

        # 跑马灯本体：亮头窄（sigma 0.05 ≈ 1/20 圈）、尾巴短（exp 衰减，约 30°）。
        # 尾巴必须收住 —— 太长就像拖了一条脏尾巴（用户反馈过「拖尾太长」）。
        sigma = 0.05
        trail = np.exp(-behind / self.marquee_trail)
        crest = np.exp(-(dist / sigma) ** 2)                     # 亮头
        rim_hi = np.exp(-((dist - sigma * 0.90) / (sigma * 0.40)) ** 2)   # 亮头前沿高光
        g = max(0.0, float(marquee_gain))
        marquee = (wave_env * (0.34 * trail + 0.90 * crest)
                   + wave_env * rim_hi * 0.25) * g

        lit = 1.0
        if wave is None:
            a = (base + marquee) * intensity
        else:
            # ---- 径向扩散：光从按钮位置向外推一圈（参考视频的核心机制）----
            # 扩散期**不要角向行波**：它会在波前后面拖出一条长尾（用户反馈过「拖尾太长」）。
            # 参考视频里波前扫过之后是「持续亮着」，不是「拖一条渐隐的尾巴」。
            a = base * intensity
            R = wave["radius"]
            bw = wave["width"]
            db = self.dbtn
            # 波前扫过之后该点才被点亮，前沿用 smoothstep 过渡
            lit = np.clip((R - db) / bw + 0.5, 0.0, 1.0)
            lit = lit * lit * (3.0 - 2.0 * lit)
            a = a * lit
            # 跟着波前走的光带：前侧宽、后侧窄 -> 拖尾更短
            dd = db - R
            bl = np.where(dd > 0.0, bw, bw * 0.5)
            band = np.exp(-(dd / bl) ** 2)
            a = a + band * (0.03 + 0.62 * fall) * intensity   # 光带本体
            rgb = np.clip(rgb + (band * 0.26)[..., None], 0.0, 1.0)

        # 白热核心：官方复刻是 inset 0 36px 64px -28px -> 8px 实心核 + 一层白雾，
        # 参考视频里边缘最内侧确实压着一条白亮带（不是纯彩色边线）。
        core = np.exp(-np.maximum(d - 1.0, 0.0) / 9.0)
        a = a + core * 0.22 * intensity * lit
        rgb = np.clip(rgb + (core * 0.30 * lit)[..., None], 0.0, 1.0)

        a = np.clip(a, 0.0, 1.0)
        out = np.concatenate([np.clip(rgb, 0.0, 1.0), a[..., None]], axis=-1)
        return _blur(out, 3.2)


class OrbRenderer(object):
    """底部 Siri 光球：螺旋彩带 + 白色核心 + 外辉。"""

    def __init__(self, size=256):
        self.size = size
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        c = (size - 1) / 2.0
        self.dx = xx - c
        self.dy = yy - c
        self.r = np.sqrt(self.dx ** 2 + self.dy ** 2) / (size / 2.0)
        self.th = np.arctan2(self.dy, self.dx) / (2.0 * np.pi)

    def render(self, t, intensity, mode="listening"):
        r = self.r
        speed = {"listening": 1.0, "thinking": 2.1, "speaking": 1.35}.get(mode, 1.0)
        spiral = self.th * 0.55 + 1.25 / (r + 0.18) - t * 0.16 * speed
        hue = np.mod(spiral * 0.9 + t * 0.04, 1.0)
        hue2 = np.mod(hue + 0.40 + 0.22 * np.sin(3.0 * self.th + t * 0.45), 1.0)
        body = np.clip(1.0 - (r / 0.98) ** 3.4, 0.0, 1.0)
        core = np.exp(-(r / 0.30) ** 2)
        halo = np.exp(-((r - 0.74) / 0.30) ** 2) * 0.30
        cloud = _hsv(hue, 0.52, 1.0) * 0.58 + _hsv(hue2, 0.42, 1.0) * 0.42
        rgb = cloud * (body ** 1.15)[..., None] * 0.92
        rgb = rgb + core[..., None] * 1.0
        rgb = rgb + _hsv(np.mod(hue + 0.5, 1.0), 0.48, 1.0) * halo[..., None]
        a = np.clip(body * 0.60 + core * 0.85 + halo * 0.55, 0.0, 1.0) * intensity
        out = np.concatenate([np.clip(rgb, 0.0, 1.0), a[..., None]], axis=-1)
        return _blur(out, 7.0)


class Overlay(QtWidgets.QWidget):
    # 麦克风电平（0..1）：由听写线程发出，驱动边光向内收缩
    sig_level = QtCore.pyqtSignal(float)

    def __init__(self, cfg):
        QtWidgets.QWidget.__init__(self)
        self.cfg = cfg
        self.level = 0.0          # 平滑后的实际电平
        self.level_target = 0.0   # 最近一次收到的电平
        self.sig_level.connect(self.set_level)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.Tool | QtCore.Qt.NoDropShadowWindowHint |
            QtCore.Qt.WindowTransparentForInput)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

        self.state = "idle"
        self.state_since = time.time()
        self.caption = ""
        self.caption_since = 0.0
        self.card = None        # 语音对话框窗口（独立顶层窗口，能点能编辑）
        self.glow = 0.0
        self.llm = None         # 热重载后重建后端列表（模型/顺序变了立刻生效）
        self._stall = 0         # 渲染层卡死的连续计数
        self._tick_errors = 0   # _tick 累计异常次数
        self._last_err = 0.0    # 上次打印 traceback 的时刻
        self._last_topmost = 0.0
        self.t0 = time.time()
        self.press_time = None
        self.glow_img = None
        self.orb_img = None

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.state_since = time.time()
            self._sync_card()

    def set_card(self, card):
        """挂上语音对话框窗口（独立顶层窗口，只有它接收鼠标）。"""
        self.card = card
        self._sync_card()

    def _sync_card(self):
        if self.card is not None:
            try:
                self.card.set_ui(self.state, self.caption)
            except Exception:
                pass

    def mark_press(self):
        """按键触发的那一刻：按钮立刻亮起，0.2 秒后光才开始从按钮扩散。"""
        self.press_time = time.time()

    def demo_wave(self, dur=2.6):
        """跑一次扩散 + 跑马灯（**不碰麦克风、不问模型**）。

        设置面板的「试一下」走这条：面板是独立进程，写 logs/demo.request，
        常驻助手轮询到就跑一次 —— 跨进程又不用开端口。
        """
        self.press_time = time.time()
        self.set_ui("thinking", "")
        QtCore.QTimer.singleShot(int(dur * 1000), self._demo_wave_end)

    def _demo_wave_end(self):
        self.set_ui("idle", "")
        self.press_time = None

    def _check_trigger(self):
        """捡起设置面板写下来的触发文件。

        第一行是时间戳；第二行有内容 => 走**完整回复链路**（问这句话）。
        """
        try:
            if not os.path.exists(cfgstore.TRIGGER_FILE):
                return False
            with open(cfgstore.TRIGGER_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.read().splitlines()]
            try:
                os.remove(cfgstore.TRIGGER_FILE)
            except Exception:
                pass
            q = ""
            for ln in lines[1:]:
                if ln:
                    q = ln
                    break
            if q:
                self._ask(q)
            else:
                self.demo_wave()
            return True
        except Exception:
            pass
        return False

    def _ask(self, text):
        """跨进程「问一句」：走和语音、打字完全一样的回复链路。

        麦克风坏着的时候，这是唯一能在屏幕上看到完整回复的办法。
        """
        th = getattr(self, "_ask_thread", None)
        if th is not None and th.isRunning():
            return
        try:
            t = ReplyThread(text, self.cfg, self.llm, parent=self)
            t.sig_ui.connect(self.set_ui)
            self._ask_thread = t
            t.start()
        except Exception:
            traceback.print_exc()

    def _reload_config(self):
        """配置文件变了就热重载 —— 调跑马灯速度不用重启常驻进程。

        ⚠️ 写坏/写一半时**保持旧配置**（不能回落到 DEFAULT_CONFIG，那会把用户的
        key、配色、开关全冲掉）。
        """
        try:
            mt = os.path.getmtime(CONFIG_PATH)
        except Exception:
            return
        if mt == getattr(self, "_cfg_mtime", None):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                new = _merge(dict(DEFAULT_CONFIG), json.load(f))
        except Exception as e:
            print("[siri_orb] config reload failed, keep old: %s" % e)
            self._cfg_mtime = mt
            return
        self.cfg = new
        self._cfg_mtime = mt
        print("[siri_orb] config reloaded (mtime changed) -> 新的设置已生效")
        # 界面语言可能变了 -> 卡片上的状态字/提示要立刻跟着换
        self._sync_card()
        # 模型/后端顺序变了要重建后端列表，否则改了也不生效（LLM 对象是启动时建的）
        if self.llm is not None:
            try:
                self.llm.apply_config(new)
            except Exception:
                pass

    def _origin(self):
        """波源（按钮）位置：归一化坐标，默认任务栏右下角电量与日期之间。"""
        o = self.cfg.get("glow_origin") or [0.9236, 0.9711]
        return (float(o[0]) * self.width(), float(o[1]) * self.height())

    def set_level(self, v):
        """麦克风电平（0..1）。起音快、消音慢，跟得上字头又不会闪。"""
        try:
            self.level_target = max(0.0, min(1.0, float(v)))
        except Exception:
            self.level_target = 0.0

    def _sim_level(self, now):
        """模拟电平：像说话一样一 syllable 一 syllable 地起伏（麦克风没信号时用来演示）。"""
        rate = float(self.cfg.get("sim_rate", 4.2) or 4.2)
        idx = int(now * rate)
        frac = now * rate - idx
        # 每个音节用哈希取一个稳定的幅度，避免逐帧抖动
        amp = 0.35 + 0.65 * ((idx * 1103515245 + 12345) % 97) / 97.0
        env = amp * (1.0 if frac < 0.55 else max(0.0, 1.0 - (frac - 0.55) / 0.30))
        return max(0.0, min(1.0, env))

    def set_caption(self, text):
        self.caption = text or ""
        if text:
            self.caption_since = time.time()
        self._sync_card()

    def set_ui(self, state, text):
        """状态与文案一起更新，避免两者错位。"""
        self.set_state(state)
        self.set_caption(text)

    def _reassert_topmost(self):
        """把顶层透明层重新挂回 DWM 合成链。

        Windows 偶尔会让顶层透明层掉出合成：窗口还在、IsWindowVisible 也是 True、
        z 序也在最上层，但屏幕上什么都不画。SetWindowPos(HWND_TOPMOST) 能修回来。
        """
        HWND_TOPMOST = -1
        flags = 0x0001 | 0x0002 | 0x0010      # NOSIZE | NOMOVE | NOACTIVATE
        for w in (self, self.card):
            if w is None:
                continue
            try:
                ctypes.windll.user32.SetWindowPos(
                    int(w.winId()), HWND_TOPMOST, 0, 0, 0, 0, flags)
            except Exception:
                pass

    def _watchdog(self, now):
        """渲染层自愈 + 置顶重申。

        自愈判据：状态已经不是 idle（说明有交互在进行），但亮度一直起不来
        => 渲染循环卡住了。重建渲染器并手动跑一次 tick。
        """
        if self.state != "idle" and self.glow <= 0.012:
            self._stall += 1
            if self._stall >= 30:              # 33ms/帧 -> 约 1 秒
                self._stall = 0
                print("[siri_orb] render layer stalled -> rebuild")
                try:
                    self._gr = None
                    self.glow_img = None
                    self.orb_img = None
                    self.glow = 0.0
                    self._tick_body()
                except Exception:
                    traceback.print_exc()
        else:
            self._stall = 0
        if now - getattr(self, "_last_topmost", 0.0) >= 3.0:
            self._last_topmost = now
            self._reassert_topmost()

    def _tick(self):
        """渲染心跳。

        ⚠️ 整体必须包 try/except：一旦抛异常，后面的 self.update() 不会执行，
        覆盖层和卡片窗口就会「窗口还在、IsWindowVisible 也是 True，但屏幕上什么都不画」
        —— 整个视觉层静默死掉，而且不留任何痕迹（2026-11-07 实测踩过）。
        现在：异常照常记 traceback（守护已把子进程 stdout/stderr 重定向到
        logs/siri_orb.log），并且照样 update()，最坏只丢一帧。
        """
        try:
            self._tick_body()
        except Exception:
            self._tick_errors += 1
            now = time.time()
            if now - getattr(self, "_last_err", 0.0) >= 5.0:
                self._last_err = now
                print("[siri_orb] render tick failed (%d times)" % self._tick_errors)
                traceback.print_exc()
            self.update()

    def _tick_body(self):
        now = time.time()
        if now - getattr(self, "_cfg_checked", 0.0) > 0.5:
            self._cfg_checked = now
            self._reload_config()
        if now - getattr(self, "_trig_checked", 0.0) > 0.4:
            self._trig_checked = now
            self._check_trigger()
        t = now - self.t0
        st = self.state
        target = {"idle": 0.0, "listening": 1.0, "thinking": 0.82, "speaking": 1.0}.get(st, 0.0)
        # 电平平滑：起音快（跟得上字头）、消音慢（不闪）
        if str(self.cfg.get("level_source", "mic")).lower() == "sim":
            self.level_target = self._sim_level(now)
        if self.level_target > self.level:
            self.level += (self.level_target - self.level) * 0.45
        else:
            self.level += (self.level_target - self.level) * 0.08
        if st == "idle" and self.caption and (now - self.caption_since) > 3.2:
            self.set_caption("")
        if target > self.glow:
            self.glow += (target - self.glow) * 0.35
        else:
            self.glow += (target - self.glow) * 0.09

        origin = self._origin()
        mq_period = float(self.cfg.get("marquee_period_sec", 6.0))
        mq_trail = float(self.cfg.get("marquee_trail", 0.09))
        if not hasattr(self, "_gr") or self._gr.w != self.width() \
                or self._gr.h != self.height() \
                or self._gr.origin != origin \
                or self._gr.marquee_period != max(0.5, mq_period) \
                or self._gr.marquee_trail != max(0.02, mq_trail):
            self._gr = GlowRenderer(self.width(), self.height(), origin=origin,
                                    marquee_period=mq_period, marquee_trail=mq_trail)
            self._or = OrbRenderer(256)

        # ---- 径向扩散波前 ----
        wave = None
        delay = float(self.cfg.get("wave_delay_sec", 0.2))
        sweep = float(self.cfg.get("wave_sweep_sec", 0.5))
        marquee_gain = 1.0
        if self.press_time is not None:
            bw = max(30.0, self.width() * float(self.cfg.get("wave_band_frac", 0.032)))
            wt = now - self.press_time - delay
            if wt < 0.0:
                # 延迟期：光还没开始，按钮先亮（radius 取负 -> 整圈不亮）
                wave = {"radius": -1.0e9, "width": bw}
            elif wt < sweep:
                tau = wt / max(1e-3, sweep)
                ease = 1.0 - (1.0 - tau) ** 1.7
                wave = {"radius": self._gr.max_r * ease, "width": bw}
            if wave is not None:
                # 扩散期不开跑马灯（那 0.6 秒的主角是径向波）；
                # 扩散结束后 0.35s 内淡入，否则会「啪」地闪一下
                marquee_gain = 0.0
            else:
                end = self.press_time + delay + sweep
                marquee_gain = min(1.0, max(0.0, (now - end) / 0.35))

        if self.glow > 0.012:
            cap = float(self.cfg.get("glow_intensity", 1.0) or 1.0)
            intensity = max(0.0, min(1.0, self.glow * cap))
            self.glow_img = _to_qimage(
                self._gr.render(t, intensity, st, wave, marquee_gain, self.level))
            if self.cfg.get("show_orb", False):
                self.orb_img = _to_qimage(self._or.render(t, self.glow, st))
            else:
                self.orb_img = None
        else:
            self.glow_img = None
            self.orb_img = None
        self._watchdog(now)
        self.update()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
        if self.glow_img is not None:
            p.drawImage(self.rect(), self.glow_img)
            self._draw_button(p)
            if self.orb_img is not None:
                s = int(self.cfg.get("orb_size", 200))
                x = (self.width() - s) // 2
                y = self.height() - s - int(self.cfg.get("orb_bottom_margin", 92))
                p.drawImage(QtCore.QRect(x, y, s, s), self.orb_img)
        # 卡片已经移到独立的 CardWindow（要能点、能编辑），这里不再画
        p.end()

    def _draw_button(self, p):
        """按钮本体：按下即亮，作为光带的起点（参考视频里是侧边按键）。"""
        gr = getattr(self, "_gr", None)
        if gr is None or self.press_time is None:
            return
        age = time.time() - self.press_time
        if age > 6.0:
            return
        ox, oy = gr.origin
        lit = max(self.glow, max(0.0, 1.0 - age / 1.2))
        if lit <= 0.01:
            return
        r = 7.0
        glow = QtGui.QRadialGradient(ox, oy, r * 3.2)
        glow.setColorAt(0.0, QtGui.QColor(255, 255, 255, int(190 * lit)))
        glow.setColorAt(0.34, QtGui.QColor(178, 214, 255, int(105 * lit)))
        glow.setColorAt(1.0, QtGui.QColor(120, 160, 255, 0))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QtCore.QRectF(ox - r * 3.2, oy - r * 3.2, r * 6.4, r * 6.4))
        p.setBrush(QtGui.QColor(255, 255, 255, int(240 * lit)))
        p.drawEllipse(QtCore.QRectF(ox - r, oy - r, r * 2, r * 2))

    def caption_geom(self):
        """卡片几何（相对覆盖层坐标）：(x, y, w, h, label, label_h, font)。

        卡片窗口和覆盖层共用这一份算法 —— 别复制成两份，改一处忘一处。
        """
        pos = str(self.cfg.get("caption_position", "top")).lower()
        fs = int(self.cfg.get("caption_font_size", 14) or 14)
        f = QtGui.QFont("Microsoft YaHei UI", fs if pos == "top" else max(9, fs - 3))
        fm = QtGui.QFontMetrics(f)
        maxw = int(self.cfg.get("caption_max_width", 760))
        br = fm.boundingRect(QtCore.QRect(0, 0, maxw, 400),
                             int(QtCore.Qt.TextWordWrap), self.caption)
        label = i18n.state_text(i18n.ui_lang(self.cfg), self.state)
        label_h = 24 if label else 0
        if pos == "top":
            w = min(maxw, max(560, br.width() + 84))
        else:
            w = min(maxw, max(260, br.width() + 56))
        h = br.height() + 44 + label_h
        x = (self.width() - w) // 2
        if pos == "top":
            y = 44
        elif self.cfg.get("show_orb", False):
            y = self.height() - int(self.cfg.get("orb_bottom_margin", 92)) - \
                int(self.cfg.get("orb_size", 200)) - h - 20
        else:
            y = self.height() - h - 96
        if y < 20:
            y = 20
        return x, y, w, h, label, label_h, f

    def _draw_caption(self, p, draw_body=True, hint=False):
        """画卡片本体。覆盖层不再调用它 —— 卡片由独立的 CardWindow 画（要能点能编辑）。"""
        x, y, w, h, label, label_h, f = self.caption_geom()
        rect = QtCore.QRectF(x, y, w, h)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 60))
        p.drawRoundedRect(rect.adjusted(-8, -8, 8, 8), 26, 26)
        p.setBrush(QtGui.QColor(20, 20, 26, 198))
        p.drawRoundedRect(rect, 18, 18)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 44), 1))
        p.drawRoundedRect(rect, 18, 18)

        if label:
            dot = QtGui.QColor(120, 220, 255) if self.state != "thinking" else QtGui.QColor(255, 190, 110)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QtCore.QRectF(rect.left() + 28, rect.top() + 13, 8, 8))
            pf = QtGui.QFont("Microsoft YaHei UI", 8)
            p.setFont(pf)
            p.setPen(QtGui.QColor(170, 220, 245))
            p.drawText(QtCore.QRectF(rect.left() + 42, rect.top() + 5, 220, 22),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, label)

        if hint:
            p.setFont(QtGui.QFont("Microsoft YaHei UI", 8))
            p.setPen(QtGui.QColor(132, 142, 168))
            p.drawText(QtCore.QRectF(rect.left() + w - 300, rect.top() + 5, 272, 22),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                       i18n.t(i18n.ui_lang(self.cfg), "card.hint"))

        if draw_body:
            p.setFont(f)
            p.setPen(QtGui.QColor(238, 238, 245))
            p.drawText(rect.adjusted(28, 18 + label_h, -28, -14),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter | QtCore.Qt.TextWordWrap,
                       self.caption)


class CardWindow(QtWidgets.QWidget):
    """语音对话框：独立顶层窗口（卡片能点、能编辑，桌面其它地方照常点）。

    为什么单独开一个窗口：主覆盖层是 WindowTransparentForInput（全屏鼠标穿透），
    卡片画在它上面就收不到点击 —— 卡片之外想点桌面，鼠标事件得照常落到下层窗口。
    要做到这点又不动全局命中测试，最稳的就是让**卡片自己是一个不穿透的小窗口**，
    罩在卡片该在的位置上（只有卡片那一小块会吃鼠标）。

    交互：点卡片 -> 进入编辑（正文变成输入框）-> 改完按 Enter 发送
          （Shift+Enter 换行，Esc 取消，点外面失焦也取消）。
    """
    sig_submit = QtCore.pyqtSignal(str)

    PAD = 8            # 给阴影留的边

    def __init__(self, cfg, host, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        self.cfg = cfg
        self.host = host          # 覆盖层：几何算法和卡片画法都在它那儿
        self.state = "idle"
        self.caption = ""
        self.editing = False
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.Tool | QtCore.Qt.NoDropShadowWindowHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)

        self.editor = QtWidgets.QTextEdit(self)
        self.editor.setAcceptRichText(False)
        self.editor.setFrameStyle(QtWidgets.QFrame.NoFrame)
        self.editor.setStyleSheet(
            "QTextEdit { background: rgba(0,0,0,0); border: none; color: #EEEEF5; }")
        self.editor.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.editor.viewport().setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.editor.hide()
        self.editor.installEventFilter(self)

    # ---------------------------------------------------------- 状态与位置
    def set_ui(self, state, text):
        self.state = state
        self.caption = text or ""
        try:
            self._place()
        except Exception:
            # 几何算不出来也不能让卡片整个消失
            traceback.print_exc()
        if self.caption:
            if not self.isVisible():
                self.show()
            self.raise_()
        else:
            if self.editing:
                self._cancel()
            self.hide()
        self.update()

    def _place(self):
        """按覆盖层算出来的卡片几何摆窗口，并把输入框摆到正文位置。"""
        x, y, w, h, label, label_h, f = self.host.caption_geom()
        self.setGeometry(self.host.x() + x - self.PAD, self.host.y() + y - self.PAD,
                         w + 2 * self.PAD, h + 2 * self.PAD)
        body = QtCore.QRect(self.PAD + 28, self.PAD + 18 + label_h,
                            w - 56, max(20, h - 18 - label_h - 14))
        self.editor.setGeometry(body)
        self.editor.setFont(f)

    # ---------------------------------------------------------- 绘制
    def paintEvent(self, ev):
        try:
            p = QtGui.QPainter(self)
            p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            p.setRenderHint(QtGui.QPainter.TextAntialiasing, True)
            x, y, w, h, label, label_h, f = self.host.caption_geom()
            # 让覆盖层那套画法按屏幕坐标画，落在本窗口里
            p.translate(self.PAD - x, self.PAD - y)
            self.host._draw_caption(p, draw_body=not self.editing, hint=True)
            p.end()
        except Exception:
            traceback.print_exc()

    # ---------------------------------------------------------- 交互
    def mousePressEvent(self, ev):
        if not self.editing:
            self.start_edit()

    def start_edit(self):
        if not self.caption:
            return
        self.editing = True
        self.editor.setPlainText(self.caption)
        self.editor.show()
        self.raise_()
        self.activateWindow()       # 让输入框拿到键盘焦点
        self.editor.setFocus()
        self.editor.selectAll()      # 第一次点=整段选中，直接打字就是替换
        self.update()

    def _cancel(self):
        if not self.editing:
            return
        self.editing = False
        self.editor.hide()
        self.editor.clear()
        self.update()

    def _submit(self):
        text = self.editor.toPlainText().strip()
        self.editing = False
        self.editor.hide()
        self.editor.clear()
        self.update()
        if text:
            self.sig_submit.emit(text)

    def eventFilter(self, obj, ev):
        if obj is self.editor:
            if ev.type() == QtCore.QEvent.KeyPress:
                if ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                    if ev.modifiers() & QtCore.Qt.ShiftModifier:
                        return False            # Shift+Enter = 换行
                    self._submit()
                    return True
                if ev.key() == QtCore.Qt.Key_Escape:
                    self._cancel()
                    return True
            elif ev.type() == QtCore.QEvent.FocusOut:
                self._cancel()
        return False


def _reply_chain(text, cfg, llm, speaker, emit):
    """文本 -> 回复（本地指令优先，否则模型）-> 播报。语音和打字两条路共用这一段。

    🈳 中英双语：
      - 本地指令按「回复语言」出中文 / 英文 / 中英对照三种说法；
      - 模型走「语言中性的基础提示词 + 按回复语言追加的语言规则」
        （见 i18n.build_system_prompt）—— 用户自己写的提示词照样生效；
      - 播报按回复语言挑一段念：中文声线念中文段，英文声线念英文段。
    """
    ui = i18n.ui_lang(cfg)
    rl = i18n.reply_lang(cfg)
    reply = None
    try:
        reply = voice_mod.try_local_command(text, lang=rl)
    except Exception:
        reply = None
    if not reply:
        emit("thinking", i18n.t(ui, "reply.thinking"))
        try:
            reply = llm.chat([{"role": "user", "content": text}],
                             system=i18n.build_system_prompt(cfg))
        except Exception as e:
            reply = i18n.t(ui, "reply.model_down") % str(e)[:70]
    emit("speaking", reply)
    try:
        speaker.speak_localized(reply, lang=rl)
    except Exception:
        try:
            speaker.speak(reply)
        except Exception:
            pass
    time.sleep(0.3)
    return reply


class ReplyThread(QtCore.QThread):
    """打字输入 -> 回复 -> 播报。走的和语音完全一样的链路。"""

    sig_ui = QtCore.pyqtSignal(str, str)

    def __init__(self, text, cfg, llm, parent=None):
        QtCore.QThread.__init__(self, parent)
        self.text = text
        self.cfg = cfg
        self.llm = llm
        self.speaker = voice_mod.Speaker(
            voice_hint=(cfg.get("tts") or {}).get("voice_hint", "Huihui"),
            rate=(cfg.get("tts") or {}).get("rate", 0))

    def run(self):
        try:
            _reply_chain(self.text, self.cfg, self.llm, self.speaker, self.sig_ui.emit)
        except Exception as e:
            self.sig_ui.emit("listening",
                             i18n.t(i18n.ui_lang(self.cfg), "reply.error") % str(e)[:90])
            time.sleep(1.2)
        finally:
            self.sig_ui.emit("idle", "")


class VoiceThread(QtCore.QThread):
    sig_ui = QtCore.pyqtSignal(str, str)
    sig_level = QtCore.pyqtSignal(float)

    def __init__(self, cfg, parent=None, llm=None):
        QtCore.QThread.__init__(self, parent)
        self.cfg = cfg
        # ⚠️ llm 必须由外部传进来复用：LLM 对象里存着「哪个后端已经拉黑」的状态
        #    （MiniMax 没余额时第一次 402 就把它拉黑），每次按键新建一个 LLM 的话
        #    这个状态活不过一次按键，等于每句话都先去撞一次墙。
        self.llm = llm if llm is not None else LLM(cfg)
        self.speaker = voice_mod.Speaker(
            voice_hint=(cfg.get("tts") or {}).get("voice_hint", "Huihui"),
            rate=(cfg.get("tts") or {}).get("rate", 0))
        self.suppress = False    # 打字输入优先时置位：语音那次的结果直接丢弃

    def _emit(self, state, text):
        if not self.suppress:
            self.sig_ui.emit(state, text)

    def run(self):
        try:
            stt = self.cfg.get("stt") or {}
            ui = i18n.ui_lang(self.cfg)
            self._emit("listening", "……")

            def _on_level(v):
                try:
                    self.sig_level.emit(float(v))
                except Exception:
                    pass

            # 🈳 听写语言：auto = 上次成功的语言优先，这次没听清就换另一种再试。
            #    ⚠ 听写进程崩溃（麦克风坏）**不重试** —— 换语言也没用，只会白等一轮。
            langs = i18n.stt_langs(self.cfg)
            info = {}
            text = ""
            for lang in langs:
                info = {}
                text = voice_mod.listen_once(
                    timeout_sec=stt.get("timeout_sec", 10),
                    language=lang,
                    on_level=_on_level,
                    info=info)
                if text and text.strip():
                    i18n.set_last_stt_lang(lang)
                    break
                if info.get("crashed"):
                    break
            if not text or not text.strip():
                if info.get("crashed"):
                    rc = info.get("rc")
                    rcs = "-" if rc is None else ("0x%08X" % (rc & 0xFFFFFFFF))
                    self._emit("listening", i18n.t(ui, "reply.mic_crash") % rcs)
                else:
                    self._emit("listening", i18n.t(ui, "reply.not_heard"))
                time.sleep(1.2)
                self._emit("idle", "")
                return
            self._emit("listening", text)
            _reply_chain(text, self.cfg, self.llm, self.speaker, self._emit)
        except Exception as e:
            self._emit("listening",
                       i18n.t(i18n.ui_lang(self.cfg), "reply.error") % str(e)[:90])
            time.sleep(1.2)
        finally:
            self._emit("idle", "")


def make_icon():
    pm = QtGui.QPixmap(64, 64)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    g = QtGui.QRadialGradient(32, 32, 31)
    g.setColorAt(0.0, QtGui.QColor(255, 255, 255, 255))
    g.setColorAt(0.35, QtGui.QColor(140, 200, 255, 255))
    g.setColorAt(0.72, QtGui.QColor(255, 120, 220, 255))
    g.setColorAt(1.0, QtGui.QColor(255, 120, 220, 0))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(1, 1, 62, 62)
    p.end()
    return QtGui.QIcon(pm)


def run_demo(overlay):
    """视觉演示：不接麦克风和模型，自动循环各状态。"""
    if i18n.ui_lang(overlay.cfg) == "en":
        script = [
            ("listening", "How wide is the market today?", 3.6),
            ("thinking", "Thinking…", 2.4),
            ("speaking", "Breadth is 45.2%, still inside the circuit-breaker zone, "
                         "so I am staying flat.", 6.0),
            ("idle", "", 1.6),
        ]
    else:
        script = [
            ("listening", "今天市场宽度多少？", 3.6),
            ("thinking", "思考中……", 2.4),
            ("speaking", "今天市场宽度 45.2%，还在熔断区间，所以我先不开仓。", 6.0),
            ("idle", "", 1.6),
        ]
    state = {"i": 0}

    def step():
        st, txt, dur = script[state["i"] % len(script)]
        if st != "idle":
            overlay.mark_press()
        overlay.set_ui(st, txt)
        state["i"] += 1
        QtCore.QTimer.singleShot(int(dur * 1000), step)

    step()


def main():
    cfg = load_config()
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    screen = app.primaryScreen()
    overlay = Overlay(cfg)
    if screen:
        overlay.setGeometry(screen.geometry())
    overlay.show()

    demo = "--demo" in sys.argv
    voice_thread = None
    reply_thread = None
    # 全进程共用一个 LLM：后端熔断状态要跨按键保留
    llm_shared = LLM(cfg)
    # 让覆盖层在配置热重载时把新的 llm 段交回去（模型/顺序改了立刻生效）
    overlay.llm = llm_shared
    # ⚠ 演示实例不许抢 PID 文件 —— 那是常驻助手的身份，
    #    被演示进程覆盖的话，设置面板会以为常驻助手没在跑。
    if not demo:
        try:
            with open(cfgstore.PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    def on_submit(text):
        """卡片里打字回车 —— 走和语音完全一样的回复链路。"""
        nonlocal reply_thread
        if voice_thread is not None and voice_thread.isRunning():
            voice_thread.suppress = True      # 打字优先，语音那次的结果丢弃
        if reply_thread is not None and reply_thread.isRunning():
            return
        # 用 overlay.cfg（热重载后的最新配置），别用启动时的快照
        reply_thread = ReplyThread(text, overlay.cfg, llm_shared)
        reply_thread.sig_ui.connect(overlay.set_ui)
        reply_thread.start()

    # 语音对话框：独立顶层窗口（能点、能编辑），桌面其它地方照常点
    card = CardWindow(cfg, overlay)
    overlay.set_card(card)
    card.sig_submit.connect(on_submit)

    def trigger():
        nonlocal voice_thread
        if voice_thread is not None and voice_thread.isRunning():
            return
        overlay.mark_press()
        # 同上：语音线程用最新配置（听写超时/播报音色/语速改了立刻生效）
        voice_thread = VoiceThread(overlay.cfg, llm=llm_shared)
        voice_thread.sig_ui.connect(overlay.set_ui)
        voice_thread.sig_level.connect(overlay.set_level)
        voice_thread.start()

    if demo:
        run_demo(overlay)
    else:
        hook = KeyHook(
            on_hotkey=trigger,
            on_match=lambda vk, sc: trigger(),
            hotkey=cfg.get("hotkey", "ctrl+alt+space"),
            scan_code=cfg.get("xiaomi_scan_code"),
            vk=cfg.get("xiaomi_vk"),
            log_path=KEY_LOG,
            log_all=bool(cfg.get("log_all_keys", True)),
            swallow=bool(cfg.get("swallow_matched_key", True)))
        hook.start()
        time.sleep(0.4)
        if hook.error:
            print("[siri_orb] hook error: %s" % hook.error)
        if hook.hotkey_error:
            print("[siri_orb] hotkey error: %s" % hook.hotkey_error)
        print("[siri_orb] key log: %s" % KEY_LOG)

    def open_settings():
        """拉起设置面板（独立进程 —— 助手没在跑也能打开改配置）。"""
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([sys.executable, os.path.join(HERE, "settings.py")],
                             cwd=HERE, creationflags=flags)
        except Exception as e:
            print("[siri_orb] cannot open settings: %s" % e)

    tray_lang = i18n.ui_lang(cfg)
    tray = None
    if cfg.get("show_tray_icon", True):
        tray = QtWidgets.QSystemTrayIcon(make_icon(), app)
        menu = QtWidgets.QMenu()
        act_test = menu.addAction(i18n.t(tray_lang, "tray.demo"))
        act_test.triggered.connect(trigger)
        act_set = menu.addAction(i18n.t(tray_lang, "tray.settings"))
        act_set.triggered.connect(open_settings)
        menu.addSeparator()
        act_quit = menu.addAction(i18n.t(tray_lang, "tray.quit"))
        act_quit.triggered.connect(app.quit)
        tray.setContextMenu(menu)
        tray.setToolTip(i18n.t(tray_lang, "tray.tooltip"))
        tray.show()
    else:
        print("[siri_orb] tray icon disabled by config -> no settings / quit entry")

    # 托盘是常驻的：改了「界面语言」要跟着改菜单文案，不能按启动时的语言定死
    def _sync_tray():
        if tray is None:
            return
        lang = i18n.ui_lang(overlay.cfg)
        if getattr(_sync_tray, "_lang", None) == lang:
            return
        _sync_tray._lang = lang
        act_test.setText(i18n.t(lang, "tray.demo"))
        act_set.setText(i18n.t(lang, "tray.settings"))
        act_quit.setText(i18n.t(lang, "tray.quit"))
        tray.setToolTip(i18n.t(lang, "tray.tooltip"))
        print("[siri_orb] tray labels -> %s" % lang)

    if tray is not None:
        _sync_tray._lang = tray_lang
        tray_timer = QtCore.QTimer()
        tray_timer.timeout.connect(_sync_tray)
        tray_timer.start(1000)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
