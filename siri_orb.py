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
import math
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
import keyhook as keyhook_mod
import win_h as winh_mod
import agent as agent_mod
import tools as tools_mod
import context as context_mod

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

CONFIG_PATH = cfgstore.CONFIG_PATH
# 日志必须落在**可写目录**（源码=脚本目录；打包后=exe 同级目录）
LOG_DIR = os.path.join((pathmod.data_dir() if pathmod else HERE), "logs")
KEY_LOG = os.path.join(LOG_DIR, "keyhook.log")
TRACE_LOG = os.path.join(LOG_DIR, "siri_orb_trace.log")


def _trace(msg):
    """落盘追踪：stdout + 独立日志文件双写。

    打包后 stdout 会被重定向到 logs/siri_orb.log（行缓冲，是可靠的）；
    再单独写一份文件，纯粹是为了日志被清掉时还能留证据。
    ⚠️ 写文件失败**必须把异常打出来** —— 静默吞掉的话，
       「追踪文件不存在」这种事实本身就成了最大的误导（2026-11-08 踩过）。
    """
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print("[trace] " + line)
    except Exception:
        pass
    try:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        with open(TRACE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception as e:
        try:
            print("[trace] file write failed: %r" % (e,))
        except Exception:
            pass

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
    """读配置。**第一次运行会自动生成一份默认配置**（打包成 exe 也一样）。

    之前是自己 open() 读的：文件不存在时只会报错，配置也永远不落盘 ——
    打包后应该在 exe 旁边自动多出一个 config.json。
    """
    try:
        return cfgstore.load()
    except Exception as e:
        print("[siri_orb] config read failed: %s" % e)
        return dict(DEFAULT_CONFIG)


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


def wave_radius(tau):
    """波前半径（占「最远角距离」的百分比，0..1）。

    ⚠️ **恒速**（线性）。这里原来写的是 1-(1-tau)^1.7（前快后慢、末端趋零）：
       波前是个圆，它撞到**离波源最远的那个角（左上角）**之后就再没有东西可推 ——
       那个位置连续 5 帧位移 = 0（钉死 66ms），用户看到的就是
       「每次扩散都卡在同一个地方」。参考视频沿边点亮的时刻也接近匀速
       （ref/wave.txt：8.20s -> 9.87s 扫完一圈）。
    ⚠️ **只此一份**：doctor / make_release_images / palette_check 都调它。
       别在别处再抄一遍公式 —— 抄出去的那份一定会有漂移。
    """
    t = float(tau)
    if t < 0.0:
        return 0.0
    if t > 1.0:
        return 1.0
    return t


def glow_visible_spans(al, thr=8, ts=8):
    """量出一帧边光的**可见区域**，压成若干条横向区段（缓冲像素）。

    返回 [(r0, r1, c0, c1), ...]（缓冲像素，r1/c1 是开区间）。

    ⚠️ **不能只按外接矩形**：四角的热斑会沿对角线外扩，
    外接矩形直接退化成整屏（2026-11-09 实测：按行取最大算出来的是
    150/150/240/240 —— 也就是「整个缓冲都有内容」）。
    所以改成**瓦片网格 + 逐行连通段**：既贴合真实形状，段数也只有 30 来个。

    判据 alpha >= thr（默认 8/255 = 3%）：比这更淡的像素人眼分辨不出来，
    那一大片区域**不用重绘**（重绘它只是白烧 CPU）。
    """
    h, w = int(al.shape[0]), int(al.shape[1])
    spans = []
    if h <= 0 or w <= 0:
        return spans
    # ⚠️ 网格必须**补齐**到 ts 的整数倍：直接按 h//ts*ts 截断的话，
    #    最后那几行（150 行缓冲里正好是底边光带所在）就掉到网格外了，
    #    那部分永远不会被重绘 —— 屏幕上会留一条不更新的旧边光。
    hh = ((h + ts - 1) // ts) * ts
    ww = ((w + ts - 1) // ts) * ts
    if hh != h or ww != w:
        padded = np.zeros((hh, ww), dtype=al.dtype)
        padded[:h, :w] = al
    else:
        padded = al
    tile = padded.reshape(hh // ts, ts, ww // ts, ts).max(axis=(1, 3))
    dirty = tile >= thr
    for i in range(dirty.shape[0]):
        cols = np.flatnonzero(dirty[i])
        if cols.size == 0:
            continue
        br = np.flatnonzero(np.diff(cols) > 1)
        starts = [0] + (br + 1).tolist()
        ends = br.tolist() + [cols.size - 1]
        for s, e in zip(starts, ends):
            spans.append((i * ts, (i + 1) * ts,
                          int(cols[s]) * ts, int(cols[e]) * ts + ts))
    return spans


def glow_span_rects(spans, scale, w, h):
    """区段（缓冲像素）-> 窗口坐标的矩形列表。"""
    out = []
    for r0, r1, c0, c1 in spans:
        x = max(0, min(int(c0 * scale), w))
        y = max(0, min(int(r0 * scale), h))
        ww = max(0, min(int((c1 - c0) * scale), w - x))
        hh = max(0, min(int((r1 - r0) * scale), h - y))
        if ww <= 0 or hh <= 0:
            continue
        out.append(QtCore.QRect(x, y, ww, hh))
    return out




def _to_qimage_u8(arr):
    """已经是 uint8 的 RGBA -> QImage（省掉一次 float<->uint8 来回转换）。"""
    a = np.ascontiguousarray(arr)
    h, w = a.shape[:2]
    img = QtGui.QImage(a.data, w, h, w * 4, QtGui.QImage.Format_RGBA8888)
    return img.copy()


def _blur_u8(arr, radius):
    """同 _blur，但直接吐 uint8 —— 热路径用。

    以前是 uint8 -> float32 -> （QImage 那一步）再 uint8，
    白白多两轮 57.6 万个浮点的转换。
    """
    a = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return a
    im = Image.fromarray(a)
    im = im.filter(ImageFilter.GaussianBlur(radius))
    return np.array(im)


def _blur_u8_buf(buf, radius):
    """Blur an already-uint8 RGBA buffer and return a fresh uint8 array."""
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return buf
    im = Image.fromarray(buf)
    return np.array(im.filter(ImageFilter.GaussianBlur(radius)))


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
        # scale = 每个缓冲像素对应多少物理像素。缓冲是 1/scale 的分辨率。
        # ⚠️ 屏幕多大 -> 缓冲多大：2880x1800 的屏在 scale=3 时缓冲是 960x600
        #    （57.6 万像素），实测 render 要 149ms/帧，整机只有 6fps。
        #    降分辨率是这里最大的一根杠杆（见 glow_scale 配置）。
        self.scale = max(1, int(scale))
        # 模糊半径按**物理像素**固定（原来 scale=3 时是 3.2 缓冲像素 = 9.6 物理像素），
        # 换分辨率时观感才不变。
        # ⚠️ 必须写成 3.2 * (3/scale) 而不是 9.6/scale：后者在 scale=3 时算出的是
        #    3.1999999999999997，PIL 会按它取不同的核大小 -> 画面差 17 LSB。
        self.blur_radius = 3.2 * (3.0 / float(self.scale))
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

        # ---- 每帧不变量：只算一次（2026-11-09 性能优化）----
        # 这些量跟时间、跟状态都无关，每帧重算纯属浪费：
        #   u          角向坐标（跑马灯的输入）
        #   corner/ctip 四角热斑
        #   core       白热核心
        # 每帧少算 2 个 exp + 1 个 mod + 十几个数组 pass。
        self._u = np.mod(self.theta - 0.125, 1.0).astype(np.float32)
        self._corner = np.exp(-self.dcorner / 130.0).astype(np.float32)
        self._ctip = (np.exp(-self.dcorner / 26.0) * 0.55).astype(np.float32)
        self._core = np.exp(-np.maximum(self.dist - 1.0, 0.0) / 9.0).astype(np.float32)
        # 白热核心的两个系数：每帧都要用，提前算好（省 2 个数组 pass）
        self._core022 = (self._core * 0.22).astype(np.float32)
        self._core030 = (self._core * 0.30).astype(np.float32)
        # 输出缓冲：直接写 uint8 RGBA，省掉「float -> uint8 -> float」来回转换
        self._out8 = np.zeros((self.lh, self.lw, 4), dtype=np.uint8)
        self._rgb_tmp = np.empty((self.lh, self.lw, 3), dtype=np.float32)
        self._a_tmp = np.empty((self.lh, self.lw), dtype=np.float32)

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

        # ---- 颜色查找表（性能关键，别删）----
        # 调色板只是 theta 的函数，跟时间无关 —— 时间只是让 theta 整体平移。
        # 以前每帧对 14.4 万个像素各求一次 PCHIP 样条 + 一次 np.select 换色，
        # 实测 **50ms/帧**，直接把 30fps 拖成 10fps（「扩散时掉帧」的根因）。
        # 现在把「theta -> RGB」烘成一张表，每帧只做查表 + 线性插值。
        # LUT_N=2048 时一格 = 1/2048 圈 = 0.18 度，肉眼绝对看不出来。
        self.lut_n = 2048
        _xs = np.arange(self.lut_n, dtype=np.float64) / self.lut_n
        _hh, _ss = self._palette(_xs)
        _rgb = _hsv(_hh, _ss, 1.0).astype(np.float32)
        # 多存一格并复制首格 -> 取 i1 = i0+1 时永远合法，不用再取模
        self._lut_r = np.concatenate((_rgb[:, 0], _rgb[:1, 0])).astype(np.float32)
        self._lut_g = np.concatenate((_rgb[:, 1], _rgb[:1, 1])).astype(np.float32)
        self._lut_b = np.concatenate((_rgb[:, 2], _rgb[:1, 2])).astype(np.float32)
        self._rgb_cache = None
        self._rgb_shift = 0.0
        # 底光缓存（按电平分档）—— 扩散期电平是常数，这一整块每帧都白算
        # ⚠️ ring / hot **也要缓存**：跑马灯分支（wave=None）要用它们。
        #    缓存命中时这两个名字不会在本地重新赋值 -> 必须从缓存取回，
        #    否则第二帧起就是 UnboundLocalError（听写/说话状态直接崩）。
        self._base_key = None
        self._fall = None
        self._base = None
        self._ring = None
        self._hot = None

    def _palette(self, theta):
        """按 theta 返回 (色相, 饱和度)。"""
        h = _pchip_eval(self._xe, self._ye_h, self._me_h, theta)
        s = _pchip_eval(self._xe, self._ye_s, self._me_s, theta)
        return h, s

    # 颜色场缓存的容差（圈）。LUT 一格 = 1/2048 = 0.000488 圈。
    RGB_TOL = 0.0005

    def _rgb_field(self, theta, shift):
        """按 theta 取颜色场（带缓存）。

        调色板只是 theta 的函数，跟时间无关 —— 时间只是让 theta 整体平移。
        而且**颜色场不用每帧重算**：调色板漂移慢得离谱
        （listening 0.0015 圈/秒、thinking 0.004 圈/秒，一帧才挪 0.00005~0.00013 圈），
        超过 RGB_TOL（≈1 格 = 0.18 度）才刷新 —— 人眼绝对看不出来。
        """
        sh = float(shift) % 1.0
        c = self._rgb_cache
        if c is not None:
            d = abs(sh - self._rgb_shift)
            if d > 0.5:
                d = 1.0 - d
            if d < self.RGB_TOL:
                return c
        n = self.lut_n
        u = np.mod(theta + sh, 1.0)
        idx = u * n
        i0 = idx.astype(np.int32)
        f = (idx - i0).astype(np.float32)
        i0 = np.mod(i0, n)
        i1 = i0 + 1                      # LUT 多存一格并复制首格 -> 不用再取模
        out = np.empty(theta.shape + (3,), dtype=np.float32)
        for k, lut in enumerate((self._lut_r, self._lut_g, self._lut_b)):
            v = np.take(lut, i0)
            v1 = np.take(lut, i1)
            out[..., k] = v + (v1 - v) * f
        self._rgb_cache = out
        self._rgb_shift = sh
        return out

    def render(self, t, intensity, mode="listening", wave=None, marquee_gain=1.0,
                level=0.0, u8=False):
        d = self.dist
        # 录声时的麦克风电平（0..1）驱动边光**向内收缩**：
        # 声音越大 -> 辉光往里钻得越深、整体越亮；声音越小 -> 收回贴边。
        # 参考视频实测：峰值时发光层钻到 15% 屏宽，收缩后回到 5-7%。
        lvl = max(0.0, min(1.0, float(level)))
        depth_scale = 1.0 + 0.95 * lvl
        bright = 0.85 + 0.55 * lvl
        # 调色板整体极慢漂移，配色身份（紫顶/粉右/橙底）要一直保持住
        speed = {"listening": 0.0015, "thinking": 0.0040, "speaking": 0.0022}.get(mode, 0.0015)
        # ⚠️ 这里以前是「逐像素求 PCHIP 样条 + np.select 换色」= 50ms/帧
        #    （float64 全程），是「扩散时掉帧」的头号根因。
        #    现在走查表（见 _rgb_field），并且颜色场带缓存。
        rgb = self._rgb_field(self.theta, t * speed)

        # u: 0=右下角, 0.125=下边, 0.25=左下, 0.375=左边, 0.5=左上角, ... 1=回到右下
        u = self._u
        # 两层结构：
        #   base = 常驻底光（整圈一直有一层淡虹彩，扫完不会黑掉）
        #   wave_env = 行进波包络（只跟着波头走）
        # 单层 exp 衰减要么拖尾铺满一圈、要么远处直接黑掉，两层才兼顾得了。
        #
        # ⚠️ fall / ring / hot / base **只跟电平有关**，跟时间、跟波前都无关。
        #    扩散期电平是常数，这一整块（1 个 exp + 12 个数组 pass）每帧重算纯属浪费
        #    —— 按电平分档缓存，命中就省 4~5ms/帧。
        key = round(lvl, 3)
        if key != self._base_key:
            fall = np.exp(-np.maximum(d - 1.0, 0.0) / (50.0 * depth_scale))
            ring = fall * 0.50 * bright                        # 边缘弥散（随音量变亮）
            hot = (self._corner * 0.40 + self._ctip * 0.25) * bright   # 四角热斑
            self._fall = fall
            self._fall_scaled = (0.03 + 0.62 * fall).astype(np.float32)
            self._base = (ring + hot) * 0.45                    # 常驻底光（保证扫完不黑）
            self._ring = ring
            self._hot = hot
            self._base_key = key
        fall = self._fall
        base = self._base
        ring = self._ring
        hot = self._hot

        # 跑马灯本体：亮头窄（sigma 0.05 ≈ 1/20 圈）、尾巴短（exp 衰减，约 30°）。
        # 尾巴必须收住 —— 太长就像拖了一条脏尾巴（用户反馈过「拖尾太长」）。
        #
        # ⚠️ **只在真的要用的时候才算**：
        #    扩散期（wave 不为 None）跑马灯增益是 0，跑马灯根本没被用到 ——
        #    3 个 exp + 十几个数组 pass 全是白算，而那正好是「扩散时掉帧」最痛的 0.7 秒。
        #    以前这里是无条件每帧算的。
        g = max(0.0, float(marquee_gain))
        marquee = 0.0
        if g > 0.0 and wave is None:
            # 一圈几秒由 config 的 marquee_period_sec 决定（thinking 更快、speaking 略快）
            # 全程恒定速度：状态之间不再变速 —— 调速只有一个旋钮（marquee_period_sec），
            # 也不会出现「听写时慢、说话时突然快起来」的不一致感
            period = self.marquee_period
            phase = (t / period) % 1.0                     # 亮头位置（顺时针：右下→下→左→上→右）
            behind = np.mod(phase - u, 1.0)                 # 0=波头，越大越早被扫过
            dist = np.minimum(behind, 1.0 - behind)         # 到波头的最近距离
            sigma = 0.05
            trail = np.exp(-behind / self.marquee_trail)
            crest = np.exp(-(dist / sigma) ** 2)             # 亮头
            rim_hi = np.exp(-((dist - sigma * 0.90) / (sigma * 0.40)) ** 2)   # 亮头前沿高光
            wave_env = ring * 0.75 + hot * 1.00             # 行进波包络（更亮）
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
            inv_bw = 1.0 / max(1e-3, bw)
            # 波前扫过之后该点才被点亮，前沿用 smoothstep 过渡
            lit = np.clip((R - db) * inv_bw + 0.5, 0.0, 1.0)
            # smoothstep: t*t*(3-2t) —— ⚠️ 右边必须用**原始** t，
            # 就地平方会让 (3-2t) 用到平方后的值，曲线就变了。
            _sq = lit * lit
            lit = _sq * (3.0 - 2.0 * lit)
            a *= lit
            # 跟着波前走的光带：前侧宽、后侧窄 -> 拖尾更短
            # （原来每帧做除法 + where 两个 pass；改成乘倒数 + 一个系数表）
            dd = db - R
            uu = dd * inv_bw
            # ⚠️ 这里**必须是 float32**：用 Python 浮点 1.0/4.0 的话，
            #    np.where 会给 float64 -> band 变双精度 -> 后面 a / rgb 全被拖成
            #    float64，连 exp 都跑双精度（慢一倍、内存流量翻倍）。
            #    整条链本来就是 float32，别在中间破一次。
            k = np.where(dd > 0.0, np.float32(1.0), np.float32(4.0))
            band = np.exp(-(uu * uu) * k)
            a += band * (self._fall_scaled * intensity)   # 光带本体
            # ⚠️ 这里不再 clip：最后统一 clip 一次，结果一模一样（加的都是非负量）
            rgb = rgb + (band * 0.26)[..., None]

        # 白热核心：官方复刻是 inset 0 36px 64px -28px -> 8px 实心核 + 一层白雾，
        # 参考视频里边缘最内侧确实压着一条白亮带（不是纯彩色边线）。
        a = a + self._core022 * intensity * lit
        rgb = rgb + (self._core030 * lit)[..., None]

        np.clip(a, 0.0, 1.0, out=a)
        if u8:
            # 直接写 uint8 RGBA：省掉 concatenate 整块拷贝 + float->uint8 的来回转换
            out8 = self._out8
            np.clip(rgb, 0.0, 1.0, out=self._rgb_tmp)   # rgb 可能是缓存数组，不能就地改
            np.multiply(self._rgb_tmp, 255.0, out=self._rgb_tmp)
            np.copyto(out8[..., 0:3], self._rgb_tmp, casting='unsafe')
            np.multiply(a, 255.0, out=self._a_tmp)
            np.copyto(out8[..., 3], self._a_tmp, casting='unsafe')
            return _blur_u8_buf(out8, self.blur_radius)
        out = np.concatenate([np.clip(rgb, 0.0, 1.0), a[..., None]], axis=-1)
        return _blur(out, self.blur_radius)


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


def _weather_cloud_path(x, y, w, h):
    """一朵云（几个椭圆 + 一条圆角底，同向子路径在 WindingFill 下自然并成一体）。"""
    path = QtGui.QPainterPath()
    path.addEllipse(QtCore.QRectF(x, y + h * 0.30, w * 0.52, h * 0.52))
    path.addEllipse(QtCore.QRectF(x + w * 0.20, y + h * 0.04, w * 0.56, h * 0.64))
    path.addEllipse(QtCore.QRectF(x + w * 0.46, y + h * 0.26, w * 0.54, h * 0.56))
    path.addRoundedRect(QtCore.QRectF(x + w * 0.08, y + h * 0.52, w * 0.84, h * 0.44),
                        h * 0.22, h * 0.22)
    return path


def _draw_weather_icon(p, kind, x, y, size):
    """天气图标：纯 QPainter 画。

    不用 emoji —— 这台机器上 emoji 走彩色字形，尺寸和配色都不受控，
    也不跟面板的白灰配色搭。
    """
    white = QtGui.QColor(243, 245, 250, 240)
    blue = QtGui.QColor(126, 196, 255, 235)
    if kind == "sun":
        r = size * 0.26
        cx, cy = x + size / 2.0, y + size / 2.0
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 214, 120, 240))
        p.drawEllipse(QtCore.QRectF(cx - r, cy - r, r * 2, r * 2))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 214, 120, 220),
                            max(1.5, size * 0.055), QtCore.Qt.SolidLine,
                            QtCore.Qt.RoundCap))
        for i in range(8):
            a = i * math.pi / 4.0
            r0, r1 = r * 1.5, r * 2.1
            p.drawLine(QtCore.QPointF(cx + math.cos(a) * r0, cy + math.sin(a) * r0),
                       QtCore.QPointF(cx + math.cos(a) * r1, cy + math.sin(a) * r1))
        return
    if kind == "cloud_sun":
        r = size * 0.17
        cx, cy = x + size * 0.27, y + size * 0.25
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 214, 120, 235))
        p.drawEllipse(QtCore.QRectF(cx - r, cy - r, r * 2, r * 2))
        p.setBrush(white)
        p.drawPath(_weather_cloud_path(x + size * 0.16, y + size * 0.30,
                                       size * 0.84, size * 0.70))
        return
    if kind == "fog":
        p.setPen(QtGui.QPen(QtGui.QColor(226, 230, 240, 225),
                            max(1.5, size * 0.075), QtCore.Qt.SolidLine,
                            QtCore.Qt.RoundCap))
        for i in range(4):
            yy = y + size * (0.24 + i * 0.19)
            p.drawLine(QtCore.QPointF(x + size * 0.08, yy),
                       QtCore.QPointF(x + size * 0.92, yy))
        return
    # 其余都是「云 + 降水/雷电」
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(white)
    p.drawPath(_weather_cloud_path(x, y, size, size * 0.62))
    if kind == "rain":
        p.setPen(QtGui.QPen(blue, max(1.5, size * 0.07), QtCore.Qt.SolidLine,
                            QtCore.Qt.RoundCap))
        for fx in (0.26, 0.50, 0.74):
            x0, y0 = x + size * fx, y + size * 0.66
            p.drawLine(QtCore.QPointF(x0, y0),
                       QtCore.QPointF(x0 - size * 0.07, y0 + size * 0.20))
    elif kind == "snow":
        p.setPen(QtGui.QPen(QtGui.QColor(214, 236, 255, 235),
                            max(1.2, size * 0.055), QtCore.Qt.SolidLine,
                            QtCore.Qt.RoundCap))
        for fx in (0.28, 0.52, 0.76):
            cx, cy = x + size * fx, y + size * 0.78
            for a in (0.0, math.pi / 2.0):
                dx, dy = math.cos(a) * size * 0.08, math.sin(a) * size * 0.08
                p.drawLine(QtCore.QPointF(cx - dx, cy - dy),
                           QtCore.QPointF(cx + dx, cy + dy))
    elif kind == "thunder":
        bolt = QtGui.QPainterPath()
        bx, by = x + size * 0.46, y + size * 0.60
        bolt.moveTo(bx + size * 0.06, by)
        bolt.lineTo(bx - size * 0.04, by + size * 0.16)
        bolt.lineTo(bx + size * 0.03, by + size * 0.16)
        bolt.lineTo(bx - size * 0.05, by + size * 0.36)
        bolt.lineTo(bx + size * 0.10, by + size * 0.18)
        bolt.lineTo(bx + size * 0.03, by + size * 0.18)
        bolt.closeSubpath()
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 210, 110, 240))
        p.drawPath(bolt)


def _draw_pin(p, x, y, s):
    """城市后面那个定位小箭头（iOS 那种实心小箭头）。"""
    path = QtGui.QPainterPath()
    path.moveTo(x, y - s * 0.5)
    path.lineTo(x + s * 0.44, y + s * 0.44)
    path.lineTo(x, y + s * 0.18)
    path.lineTo(x - s * 0.44, y + s * 0.44)
    path.closeSubpath()
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(120, 200, 255, 230))
    p.drawPath(path)


class Overlay(QtWidgets.QWidget):
    # 触发键的回调跑在 keyhook 线程上，GUI 的活必须靠这个信号搬回主线程
    sig_trigger = QtCore.pyqtSignal()

    # 麦克风电平（0..1）：由听写线程发出，驱动边光向内收缩
    sig_level = QtCore.pyqtSignal(float)

    # 天气面板的数据（工具线程 -> GUI 线程）
    # ⚠️ 必须走信号：天气工具跑在 agent 的工作线程里，
    #    直接调 GUI 方法会被 Qt 丢掉（卡片不重绘 = 面板不上屏）。
    sig_weather = QtCore.pyqtSignal(object)

    def do_winh(self):
        """Win+H 流程 —— **必须在 GUI 线程里跑**。

        keyhook 的回调跑在它自己的线程上：
          - card.show() / setFocus() 跨线程会**静默失效**（窗口不出现）
          - QTimer.singleShot 在没有 Qt 事件循环的线程里**永远不会触发**
            （Win+H 压根发不出去）
        所以用信号把活搬回主线程再干。
        """
        import threading
        _trace("do_winh entered  thread=%s  card=%s" % (
            threading.current_thread().name,
            "yes" if self.card is not None else "NO"))
        if self.card is not None:
            _begin_winh(self, self.card)

    def __init__(self, cfg):
        QtWidgets.QWidget.__init__(self)
        self.cfg = cfg
        # 配置热重载的基线：启动时先记下当前 mtime。
        # ⚠️ 不记的话，第一次轮询必然被当成「配置变了」-> 每次启动都打一条
        #    「新的设置已生效」的假消息，看着像有东西在偷偷改配置。
        try:
            self._cfg_mtime = os.path.getmtime(CONFIG_PATH)
        except Exception:
            self._cfg_mtime = None
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
        # 按钮亮起的时刻（长按手势：按下就先亮它当反馈，但还不触发）
        self.button_time = None
        self._hold_start = 0.0      # 长按手势：按下时刻
        self._hold_until = 0.0      # 长按手势：触发时刻
        self.glow_img = None
        self.orb_img = None
        # ---- 局部重绘（只画脏矩形）----
        # 边光只贴在四条边上，中间那一大片是透明的 —— 整屏重绘纯属浪费。
        self._spans = None       # 这一帧看得见的区段（缓冲像素）
        self._prev_spans = None   # 上一帧的区段（也得重画，免得留残影）
        self._force_full = 0      # 还差几帧要整屏重绘（波环收尾用）
        self._wave_rect = None    # 波环占据的矩形
        # 天气面板（Live Activity 式）：有值时卡片画天气而不是文字
        self.weather = None
        # 面板答完话后还留在屏幕上的截止时间（0 = 不留）
        self._weather_until = 0.0
        # 用户这一轮问的原话 —— 天气面板的标题用它
        self.last_question = ""

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._fps_n = 0
        self._rnd_ms = 0.0
        self._fps_mark = time.time()
        # ---- 帧率诊断（2026-11-09）----
        # 「画一帧」和「算一帧」分开量，才知道钱在哪：
        #   render = 算（numpy + PIL，在我们的进程里）
        #   paint  = 画（GUI 线程）+ Windows 分层窗口合成/上传（内核/DWM）
        # 实测（1440x900 / scale=6）：render 8ms、paint ~13.5ms
        #   -> 合成层那 13.5ms **多核碰不到**（不在我们进程里）。
        self._applied = 0      # 真正上屏的帧数
        self._paint_ms = 0.0   # 画一帧累计耗时
        self._paint_n = 0
        # 30fps 是自己给自己限速：屏幕是 90Hz，定时器 33ms 等于白扔 2/3 的帧。
        # 真正的上限由「一帧要多久」决定（render + 光栅 + 分层窗口上传）。
        self.timer.start(11)

    def set_state(self, state):
        if state != self.state:
            old = self.state
            self.state = state
            self.state_since = time.time()
            if state == "idle":
                # 一轮结束：**不立刻收掉天气面板** —— 照 iOS Live Activity 的做法，
                # 答完话面板还留在屏幕上（weather_linger_sec），到点自己收。
                if self.weather:
                    self._weather_until = time.time() + float(
                        self.cfg.get("weather_linger_sec", 25) or 25)
            elif old == "idle" and self.weather:
                # 新的一轮开始了：上一轮的面板作废
                self.weather = None
                self._weather_until = 0.0
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
        self.button_time = self.press_time
        self._hold_start = 0.0
        self._hold_until = 0.0

    def mark_hold_start(self, hold_sec=0.5):
        """长按手势：先亮按钮当反馈，但**不开卡片、不扩散**。

        等按住满 hold_sec 才真的触发（见 keyhook.HoldGesture）。
        """
        now = time.time()
        self.button_time = now
        self._hold_start = now
        self._hold_until = now + max(0.05, float(hold_sec))

    def mark_hold_end(self):
        """短按结束（没到阈值就松手）：什么都不触发，只把按住时的余晖收掉。"""
        self._hold_start = 0.0
        self._hold_until = 0.0
        self.button_time = 0.0

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
        self.last_question = text            # 天气面板的标题就是这一句
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

    def _ensure_gr(self):
        """拿到（必要时重建）渲染器。

        重建条件：尺寸 / glow_scale / 波源位置 / 跑马灯参数 变了。
        ⚠️ 只有这一份 —— warmup() 和 _tick_body 都调它。
        """
        origin = self._origin()
        mq_period = float(self.cfg.get("marquee_period_sec", 6.0))
        mq_trail = float(self.cfg.get("marquee_trail", 0.09))
        # glow_scale：每个缓冲像素对应多少物理像素。
        # 6 = 缓冲 480x300（2880x1800 的屏）。实测 render 149ms -> 25ms，整机 6fps -> 25fps。
        # 想吃满画质改成 3（缓冲 960x600），代价是帧率掉到 6fps。
        gscale = max(2, int(self.cfg.get("glow_scale", 6) or 6))
        if getattr(self, "_gr", None) is None or self._gr.w != self.width() \
                or self._gr.h != self.height() \
                or self._gr.scale != gscale \
                or self._gr.origin != origin \
                or self._gr.marquee_period != max(0.5, mq_period) \
                or self._gr.marquee_trail != max(0.02, mq_trail):
            self._gr = GlowRenderer(self.width(), self.height(), scale=gscale,
                                    origin=origin,
                                    marquee_period=mq_period, marquee_trail=mq_trail)
            self._or = OrbRenderer(256)
        return self._gr

    def warmup(self):
        """预热渲染路径 —— **启动时跑一次**，把冷启动开销挪到没人按键的时候。

        实测（2026-11-09）：同一个进程里第一次 render() 要 **53ms**
        （PIL 高斯模糊的首次调用 + numpy 首次索引 + 颜色 LUT 的首次路径），
        之后每帧只要 2.6-3.1ms —— 差 20 倍。
        而渲染只在 glow > 0.012 时才发生，所以那 53ms 正好砸在
        **第一次按 AI 键**的那一帧上，用户看到的就是「第一次扩散卡一下」。
        """
        try:
            gr = self._ensure_gr()
            arr = gr.render(0.0, 1.0, "listening",
                            {"radius": 10.0, "width": 40.0}, 0.0, 0.0, u8=True)
            _to_qimage_u8(arr)          # 顺带把 QImage 那条路也热掉
            self._spans_from(arr)
            _trace("warmup: render path primed")
        except Exception as e:
            _trace("warmup failed: %r" % (e,))

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
        if self.weather:
            # 天气面板在场时，模型的文字回答不抢版面
            # （跟 iOS 的 Live Activity 一个做法：结构化面板就是答案，文字只播报）
            # ⚠️ 连 idle 时那句空文案也要挡 —— 挡了卡片才不会被收掉，
            #    面板才能按 iOS 的样子在屏幕上多留一会儿。
            return
        self.caption = text or ""
        if text:
            self.caption_since = time.time()
        self._sync_card()

    def set_weather(self, payload):
        """把天气数据推上屏幕（Live Activity 式面板）。

        ⚠️ 只准在 GUI 线程里调 —— 工作线程请走 sig_weather.emit()。
        """
        if not payload:
            return
        self.weather = dict(payload)
        # 标题 = 用户这一轮的原话；没有就用城市名兜底
        #（空标题会让卡片被收掉，面板就看不见了）
        self.caption = self.last_question or self.weather.get("city") or ""
        self.caption_since = time.time()
        self._weather_until = 0.0
        _trace("weather panel set: %s %s/%s %s" % (
            self.weather.get("city"), self.weather.get("hi"),
            self.weather.get("lo"), self.weather.get("text")))
        self._sync_card()
        self.update()

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
        if (st == "idle" and self.weather and self._weather_until
                and now > self._weather_until):
            # 面板 linger 到了：收掉，卡片跟着走
            self.weather = None
            self._weather_until = 0.0
            self.caption = ""
            self._sync_card()
        if target > self.glow:
            self.glow += (target - self.glow) * 0.35
        else:
            self.glow += (target - self.glow) * 0.09

        self._ensure_gr()

        # ---- 径向扩散波前 ----
        delay = float(self.cfg.get("wave_delay_sec", 0.2))
        sweep = float(self.cfg.get("wave_sweep_sec", 0.5))
        band_w = max(30.0, self.width() * float(self.cfg.get("wave_band_frac", 0.032)))
        wave, marquee_gain = self._wave_state(now, self.press_time, delay, sweep,
                                              self._gr.max_r, band_w)

        # 长按手势期间（还没到触发点）：也要出画面，否则按钮亮不起来
        hold_active = self._hold_until > 0.0 and time.time() < self._hold_until
        if self.glow > 0.012 or hold_active:
            cap = float(self.cfg.get("glow_intensity", 1.0) or 1.0)
            intensity = max(0.0, min(1.0, self.glow * cap))
            # ⚠️ 渲染**必须留在 GUI 线程**。2026-11-09 实测过「挪到工作线程」：
            #    Python 的 GIL 让它没法和 Qt 画图重叠，反而**两边都变慢**：
            #    render 8ms -> 56.9ms、paint 13.5ms -> 41.1ms、45fps -> 16fps。
            #    原因：numpy 每个 ufunc 都要重新抢一次 GIL（默认切换间隔 5ms），
            #    一帧二十多次来回 = 五十几毫秒的「护航效应」。
            #    -> 要提速只能**减少总工作量**（少画 / 少算），不能靠并行。
            _r0 = time.perf_counter()
            _arr = self._gr.render(now - self.t0, intensity, st, wave, marquee_gain,
                                   self.level, u8=True)
            self.glow_img = _to_qimage_u8(_arr)
            # 只画看得见的那几条边带 -> 先量出可见范围（按 alpha 算）
            self._spans = self._spans_from(_arr)
            self._wave_rect = self._wave_bbox(wave)
            # 真实帧率 + render 耗时：以前只能靠猜（定时器 33ms 把一切伪装成 30fps）
            self._fps_n += 1
            self._applied += 1
            self._rnd_ms += (time.perf_counter() - _r0) * 1000.0
            if now - self._fps_mark > 2.0:
                _span = max(1e-3, now - self._fps_mark)
                _trace("perf: %.1f fps(img) | %.1f fps(tick) | render %.1f ms"
                       " | paint %.1f ms | scale=%d"
                       % (self._applied / _span,
                          self._fps_n / _span,
                          self._rnd_ms / max(1, self._applied),
                          self._paint_ms / max(1, self._paint_n),
                          getattr(getattr(self, "_gr", None), "scale", 0)))
                self._fps_n = 0
                self._applied = 0
                self._rnd_ms = 0.0
                self._paint_ms = 0.0
                self._paint_n = 0
                self._fps_mark = now
            if self.cfg.get("show_orb", False):
                self.orb_img = _to_qimage(self._or.render(t, self.glow, st))
            else:
                self.orb_img = None
            self._watchdog(now)
            self._repaint(self._dirty_rects())
        else:
            # ⚠️ 什么都不亮就别 update() —— 覆盖层是**全屏半透明**窗口，
            #    每次 update() 都得让 DWM 重新合成一整层 2880x1800 的透明层。
            #    以前空闲时也一直重绘，实测白烧 **25% 单核**（2026-11-09 实测）。
            if self.glow_img is not None:
                self.glow_img = None
                self.orb_img = None
                # 收工要**整屏**擦干净（边带之外可能还留着波环的残影）
                self._prev_spans = None
                self._spans = None
                self._force_full = 0
                self.update()
            self._watchdog(now)

    def _wave_state(self, now, press_time, delay, sweep, max_r, band_w):
        """径向扩散波前 + 跑马灯增益。**在工作线程里算**，时间基准取 worker 自己的钟。

        为什么必须在 worker 里算：波前 0.5 秒扫完全屏（≈3180 物理 px/s），
        流水线会引入 ~20ms 的滞后 —— 拿 GUI 线程的时间去算，
        光带会明显落后于按钮位置（滞后量比光带本身还宽）。
        取 worker 真正开算的那一刻为准，滞后就只剩显示本身的延迟。
        """
        wave = None
        marquee_gain = 1.0
        if press_time is not None:
            wt = now - press_time - delay
            if wt < 0.0:
                # 延迟期：光还没开始，按钮先亮（radius 取负 -> 整圈不亮）
                wave = {"radius": -1.0e9, "width": band_w}
            elif wt < sweep:
                tau = wt / max(1e-3, sweep)
                # ⚠️ **恒速**（线性半径）。别再用 1-(1-tau)^1.7 这种「前快后慢」的缓动：
                #    末端速度趋零 -> 波前是个圆，它撞到**离波源最远的那个角（左上角）**
                #    之后就没有任何东西可以再推进了 —— 实测那里连续 5 帧位移 = 0，
                #    整整钉死 66ms，用户看到的就是「每次扩散都卡在同一个地方」。
                #    参考视频沿边点亮的时刻也接近匀速（ref/wave.txt：8.20s -> 9.87s
                #    扫完一圈，t=9.0s 时半径只到全程的 28-46%），1.7 次方反而太快。
                #    自检见 wave_motion_check.py，公式只有 wave_radius() 这一份。
                wave = {"radius": max_r * wave_radius(tau), "width": band_w}
            if wave is not None:
                # 扩散期不开跑马灯（那 0.6 秒的主角是径向波）；
                # 扩散结束后 0.35s 内淡入，否则会「啪」地闪一下
                marquee_gain = 0.0
            else:
                end = press_time + delay + sweep
                marquee_gain = min(1.0, max(0.0, (now - end) / 0.35))
        return wave, marquee_gain

    # ---------------------------------------------------------- 局部重绘
    def _spans_from(self, arr):
        """可见区段（缓冲像素）—— 算法在模块级 glow_visible_spans，便于无 GUI 自检。"""
        thr = int(self.cfg.get("dirty_thr", 8) or 8)
        ts = int(self.cfg.get("dirty_tile", 8) or 8)
        return glow_visible_spans(arr[..., 3], thr, ts)

    def _wave_bbox(self, wave):
        """扩散波环占据的矩形（窗口坐标）；没有波环返回 None。

        波环是大圆环，外接矩形几乎覆盖整屏 —— 所以扩散期间干脆整屏重绘
        （那时候本来也没有省下来的空间）。
        """
        if wave is None:
            return None
        r = float(wave.get("radius", 0.0))
        w = float(wave.get("width", 0.0))
        if r < -1e8:
            return None
        ox, oy = self._origin()
        rc = QtCore.QRect(int(ox - r - w), int(oy - r - w),
                          int(2 * (r + w)), int(2 * (r + w)))
        return rc.intersected(self.rect())

    def _dirty_rects(self):
        """这一帧要重绘的矩形列表；None = 整屏重绘。

        2026-11-09 实测（1440x900 / scale=6，探针 A/B）：
          整屏 paint 27.7ms -> 只画四条边带 13.0ms（正好是面积比 47%）。
        ⚠️ 但**上传**（UpdateLayeredWindow）用的是脏区的**外接矩形**，
        四条带的外接矩形还是整屏 —— 所以省下的是光栅那一半，
        上传那一半还在（Windows 的限制，不是 Qt 的）。
        """
        if not bool(self.cfg.get("dirty_rect", True)):
            self._prev_spans = None
            return None
        spans = self._spans
        if spans is None:
            return None
        if self._wave_rect is not None:
            # 波环期间整屏重绘；波环刚消失的那一帧也要整屏重绘一次，
            # 否则环的残影会留在屏幕中间。
            self._force_full = 1
            self._prev_spans = None
            return None
        if self._force_full > 0:
            self._force_full -= 1
            self._prev_spans = None
            return None
        gr = getattr(self, "_gr", None)
        s = float(getattr(gr, "scale", 6) or 6)
        W = int(self.width())
        H = int(self.height())
        rects = glow_span_rects(spans, s, W, H)
        prev = self._prev_spans
        if prev:
            # 上一帧画过的地方这一帧也要重画 —— 光会往里缩，
            # 只画新出现的那一圈的话，外侧会留一圈脏边。
            rects += glow_span_rects(prev, s, W, H)
        # 按钮（波源）在右下角、光球在底部中间 —— 它们不是缓冲里的内容，
        # 得单独把范围算进脏区。
        ox, oy = self._origin()
        rects.append(QtCore.QRect(int(ox) - 26, int(oy) - 26, 52, 52)
                     .intersected(self.rect()))
        if self.orb_img is not None:
            sz = int(self.cfg.get("orb_size", 200) or 200)
            mg = int(self.cfg.get("orb_bottom_margin", 92) or 92)
            rects.append(QtCore.QRect(int(self.width() / 2) - sz // 2 - 4,
                                      H - mg - sz - 4, sz + 8, sz + 8))
        self._prev_spans = spans
        return rects

    def _repaint(self, rects):
        """按脏区重绘；rects=None 就是整屏重绘。

        Qt 会用**脏区**去 clip 画笔 —— 实测 clip 确实生效：
        只画底部 300px 那一条时，paint 从 27.7ms 掉到 9.1ms（面积比 33%）。
        """
        if rects is None:
            self.update()
            return
        for rc in rects:
            if rc.width() > 0 and rc.height() > 0:
                self.update(rc)

    def paintEvent(self, ev):
        _p0 = time.perf_counter()
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
        # 画一帧要多久 —— 这是流水线的另一半（render 在工作线程，这里是 GUI 线程）
        self._paint_ms += (time.perf_counter() - _p0) * 1000.0
        self._paint_n += 1

    def _draw_button(self, p):
        """按钮本体：按下即亮，作为光带的起点（参考视频里是侧边按键）。"""
        gr = getattr(self, "_gr", None)
        bt = self.button_time or self.press_time
        if gr is None or bt is None:
            return
        age = time.time() - bt
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
        # ---- 长按进度环 ----
        # 按住小米键时绕按钮画一圈进度，转满 = 触发（2026-11-09 加）
        if self._hold_start > 0.0 and self._hold_until > self._hold_start:
            now = time.time()
            if now < self._hold_until:
                prog = (now - self._hold_start) / (self._hold_until - self._hold_start)
                prog = max(0.0, min(1.0, prog))
                rp = r * 3.0
                p.setPen(QtGui.QPen(QtGui.QColor(150, 220, 255, 210), 2.0))
                p.setBrush(QtCore.Qt.NoBrush)
                p.drawArc(QtCore.QRectF(ox - rp, oy - rp, rp * 2, rp * 2),
                          int(-90 * 16), int(prog * 360 * 16))
                p.setPen(QtCore.Qt.NoPen)

    def caption_geom(self):
        """卡片几何（相对覆盖层坐标）：(x, y, w, h, label, label_h, font)。

        卡片窗口和覆盖层共用这一份算法 —— 别复制成两份，改一处忘一处。
        """
        pos = str(self.cfg.get("caption_position", "top")).lower()
        fs = int(self.cfg.get("caption_font_size", 14) or 14)
        f = QtGui.QFont("Microsoft YaHei UI", fs if pos == "top" else max(9, fs - 3))
        fm = QtGui.QFontMetrics(f)
        maxw = int(self.cfg.get("caption_max_width", 760))
        # ⚠️ 高度上限别卡太死：多步骤任务的回答本来就长，
        #    卡死了卡片就只显示中间那一段（用户看到「只展示一点」）。
        br = fm.boundingRect(QtCore.QRect(0, 0, maxw, 900),
                             int(QtCore.Qt.TextWordWrap), self.caption)
        label = i18n.state_text(i18n.ui_lang(self.cfg), self.state)
        label_h = 24 if label else 0
        if self.weather:
            # 天气面板：外面一个大圆角块，里面再套一个（照 iOS Live Activity 的版式）
            w = min(maxw, max(680, 860))
            h = 158 + label_h
        else:
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
        # ⚠️ 卡片底色要够实 —— 200/198 太透，背后的字会清清楚楚透出来，
        #    看着像「贴了层滤镜」而不是 iOS 那种 Live Activity 材质（2026-11-08 实测）。
        p.setBrush(QtGui.QColor(18, 18, 24, 240))
        p.drawRoundedRect(rect, 18, 18)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 44), 1))
        p.drawRoundedRect(rect, 18, 18)

        if self.weather:
            # 天气面板：标题 + 内层圆角块（状态字挪到标题行右边）
            self._draw_weather(p, rect, label, label_h)
            return

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
            # 顶对齐：实在放不下时显示**开头**（顶对齐显示中间那段更没用）
            p.drawText(rect.adjusted(28, 18 + label_h, -28, -14),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop | QtCore.Qt.TextWordWrap,
                       self.caption)

    def _draw_weather(self, p, rect, label, label_h):
        """Live Activity 式天气面板（版式照 iOS 18 的天气实时活动）。

            ┌ 外层大圆角块 ────────────────────┐
            │ 今天天气怎么样 ↗        ● 说话中 │  ← 标题 = 你问的那句
            │ ┌ 内层圆角块 ─────────────────┐ │
            │ │ 上海市📍            [☁]     │ │
            │ │ 18° 14°              微雨   │ │
            │ │                        今天   │ │
            │ └────────────────────────────┘ │
            └─────────────────────────────────┘
        """
        lang = i18n.ui_lang(self.cfg)
        w = rect.width()
        title = self.caption or ""

        # ---- 标题行：左边是你的问题，右边是状态
        tf = QtGui.QFont("Microsoft YaHei UI", 10)
        p.setFont(tf)
        p.setPen(QtGui.QColor(238, 240, 247, 238))
        p.drawText(QtCore.QRectF(rect.left() + 26, rect.top() + 12, w - 170, 24),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, title)
        if title:
            # 标题后面那个「↗」（参考里就有）
            tx = rect.left() + 26 + QtGui.QFontMetrics(tf).horizontalAdvance(title) + 6
            p.setPen(QtGui.QColor(214, 218, 230, 190))
            p.drawText(QtCore.QRectF(tx, rect.top() + 12, 24, 24),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, "↗")
        if label:
            dot = (QtGui.QColor(120, 220, 255) if self.state != "thinking"
                   else QtGui.QColor(255, 190, 110))
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(dot)
            p.drawEllipse(QtCore.QRectF(rect.right() - 124, rect.top() + 19, 8, 8))
            p.setFont(QtGui.QFont("Microsoft YaHei UI", 8))
            p.setPen(QtGui.QColor(170, 220, 245))
            p.drawText(QtCore.QRectF(rect.right() - 112, rect.top() + 11, 86, 24),
                       QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, label)

        # ---- 内层圆角块
        ir = QtCore.QRectF(rect.left() + 18, rect.top() + 42 + label_h,
                            w - 36, rect.height() - 60 - label_h)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255, 30))
        p.drawRoundedRect(ir, 16, 16)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 32), 1))
        p.drawRoundedRect(ir, 16, 16)

        data = self.weather or {}

        # ---- 左边：城市 + 大字温度
        city = data.get("city") or ""
        cf = QtGui.QFont("Microsoft YaHei UI", 11)
        p.setFont(cf)
        p.setPen(QtGui.QColor(233, 236, 245, 225))
        p.drawText(QtCore.QRectF(ir.left() + 20, ir.top() + 6, 340, 24),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, city)
        _draw_pin(p, ir.left() + 20 + QtGui.QFontMetrics(cf).horizontalAdvance(city) + 9,
                  ir.top() + 18, 13)
        hi, lo = data.get("hi"), data.get("lo")
        if hi is not None:
            big = "%d°" % round(float(hi))
            bf = QtGui.QFont("Microsoft YaHei UI", 22)
            p.setFont(bf)
            p.setPen(QtGui.QColor(246, 247, 252))
            x1 = ir.left() + 20
            p.drawText(QtCore.QRectF(x1, ir.top() + 38, 220, 46),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, big)
            if lo is not None:
                p.setFont(QtGui.QFont("Microsoft YaHei UI", 17))
                p.setPen(QtGui.QColor(198, 203, 215))
                dx = x1 + QtGui.QFontMetrics(bf).horizontalAdvance(big) + 12
                p.drawText(QtCore.QRectF(dx, ir.top() + 44, 160, 40),
                           QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                           "%d°" % round(float(lo)))

        # ---- 右边：图标 + 天气 + 今天
        isz = 34
        ix = ir.right() - 22 - isz
        iy = ir.top() + 6
        _draw_weather_icon(p, data.get("icon") or "cloud", ix, iy, isz)
        p.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
        p.setPen(QtGui.QColor(236, 238, 246, 225))
        text = data.get("text_en") if lang == "en" else data.get("text")
        p.drawText(QtCore.QRectF(ix - 130, iy + isz + 2, 130 + isz, 22),
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, text or "")
        p.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
        p.setPen(QtGui.QColor(186, 191, 205, 170))
        p.drawText(QtCore.QRectF(ix - 130, iy + isz + 26, 130 + isz, 20),
                   QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter,
                   i18n.t(lang, "weather." + (data.get("day") or "today")))


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
        # 抢焦点之前的前台窗口：用完要还回去
        self._prev_fg = 0
        self._stole_focus = False
        # close_card 的重入保护：我们自己发的 Esc 会走回 _cancel() -> close_card()
        self._closing = False
        # 最近一次输入法事件的时刻（IME 提交时那一下回车是注入的，要放行）
        self._ime_until = 0.0
        # Win+H 阶段的兜底计时器：没人说话也没人确认，卡片不能一直挂着抢键盘
        self._winh_timer = QtCore.QTimer(self)
        self._winh_timer.setSingleShot(True)
        self._winh_timer.timeout.connect(self._winh_timeout)
        self._winh_cb = None
        # 「麦克风好了没」的探针：面板从按键到就绪要 1~2 秒，
        # 而面板又被我们藏起来了 -> 卡片上的提示字是用户唯一的信号。
        self._live_timer = QtCore.QTimer(self)
        self._live_timer.setInterval(200)
        self._live_timer.timeout.connect(self._poll_voice_live)
        self._live_state = None
        # 「麦克风已就绪」是**闩**：这一轮见过会话活过就一直算就绪。
        # 会话的 Active 状态会闪（面板被我们藏起来、藏/显切换时音频会话
        # 瞬时 inactive —— 2026-11-09 19:52 实测 33 秒活、34 秒又死），
        # 跟着它翻 = 提示字在「正在打开…」和「麦克风已就绪」之间抖，
        # 用户根本判断不了该什么时候开口。
        self._live_seen = False
        # 「麦克风已就绪」是**闩**：会话活过 + 静音解除，才算就绪。
        self._ready = False
        # 还焦点用的**非阻塞**轮询：语音会话还活着时不能把键盘还给用户，
        # 但**绝不能阻塞 GUI 线程去等**（2026-11-09 实测：阻塞 2.5 秒 = 整个 app 卡死）。
        self._restore_hwnd = 0
        self._restore_deadline = 0.0
        self._restore_timer = None
        # 「多久没动静就放弃」的滑动超时器：
        #   每次有识别结果进来 / 麦克风有信号 -> 截止时间往后推。
        self._timeout_sec = 0.0
        self._timeout_hard_deadline = 0.0
        # 麦克风电平监测线程只写这一个时间戳（不碰 Qt）
        self._speech_until = 0.0
        self._watch_thread = None
        self._watch_stop = True
        try:
            _stt = (cfg.get("stt") or {})
            self._detect_speech = bool(_stt.get("detect_speech", True))
        except Exception:
            self._detect_speech = True
        self.editor.textChanged.connect(self._on_text_changed)

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
            self.close_card()
        self.update()

    def close_card(self):
        """卡片的**唯一出口**：藏卡片 + 还焦点。

        ⚠️ 以前「还焦点」只挂在 set_ui() 的空文案分支上，可关闭路径有 6~7 条
        （提交 / Esc / 失焦 / Win+H 超时 / 天气面板 linger 结束 / 空文案 / 兜底），
        只有 1 条会走到 —— 2026-11-08 日志：57 次抢焦点只还了 7 次，
        用户侧表现就是「键盘失灵，字打到别处」。
        现在所有路径都走这里，restore_foreground() 自己幂等（还过就清了标记）。

        ⚠️ 2026-11-09 加了一步：**先把还活着的 Windows 语音输入会话关掉**。
        那个会话会一直按着键盘 —— 卡片收了、焦点还给用户了，可键盘还被系统
        语音输入按着，用户敲的字被吃掉，表现就是「别的应用没有反馈」
        （2026-11-09 实测：只能靠人手动按 Esc 才恢复）。
        """
        if self._closing:
            return
        self._closing = True
        try:
            self.cancel_timeout()
            # 关掉还活着的语音会话（只在这个会话真的活着时才发 Esc）
            try:
                if winh_mod is not None and winh_mod.dismiss_voice_session():
                    _trace("close_card: dismissed the live voice session")
            except Exception:
                pass
            self.editor.hide()
            self.editor.clear()
            self.editing = False
            if self.isVisible():
                self.hide()
        except Exception:
            traceback.print_exc()
        finally:
            self._closing = False
            # 抢了焦点就必须还 —— 放 finally 里，异常也不能吞掉
            self.restore_foreground()

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

    def start_edit(self, placeholder=None):
        """进入编辑态。

        placeholder 给了：输入框**清空**，用它当灰色提示文字。
        Win+H 模式要这个 —— 提示词不能混进用户说的话里；
        而且「什么都没识别到」时内容是空的，按 Enter 也不会误发。
        """
        if not self.caption and placeholder is None:
            return
        self.editing = True
        # 新的一轮：把「麦克风已就绪」的闩复位
        self._live_seen = False
        self._ready = False
        self._live_state = None
        if placeholder is None:
            self.editor.setPlainText(self.caption)
            self.editor.setPlaceholderText("")
        else:
            self.editor.setPlainText("")
            self.editor.setPlaceholderText(placeholder)
        self.editor.show()
        self.raise_()
        self.activateWindow()       # 让输入框拿到键盘焦点
        self.editor.setFocus()
        if placeholder is None:
            self.editor.selectAll()  # 第一次点=整段选中，直接打字就是替换
        self.update()

    def _poll_voice_live(self):
        """语音会话起来了没？—— 卡片上的提示字跟着变。

        为什么必须有：Windows 的语音输入面板是**被我们藏起来的**
        （用户要求不刷屏），所以用户看不到「麦克风好了没」，
        只能靠卡片上这行字。面板从按键到就绪要 1~2 秒，
        这段时间里开口 = 第一个字被静音窗口吃掉。

        ⚠️ 「就绪」必须**闩住**：见一次活就一直是就绪，不能跟着会话状态翻。
        2026-11-09 19:52 实测：面板被我们藏起来/藏显切换时，音频会话的
        Active 会瞬时闪断（19:52:33 活 -> 19:52:34 又死），照着它翻 =
        提示字抖动，用户永远等不到一个稳定的「可以开口了」。
        """
        if not self.editing:
            return
        try:
            live = bool(winh_mod is not None and winh_mod.voice_ui_active())
        except Exception:
            live = False
        if live:
            self._live_seen = True      # 闩住：这一轮见过活 = 面板开过
        # ⚠️ 光「会话活着」还不够：静音窗口（用来盖住开始录音那声「叮」）
        #   **连麦克风采集一起压**，静音没解除之前开口 = 一个字都收不到。
        #   所以「麦克风已就绪」必须等静音真的解除。
        # ⚠️ 自愈：静音窗口重叠（连按两次 AI 键）会让语音输入宿主的会话**永久静音**，
        #   麦克风从此是死的 —— 用户说话一个字都收不到，表现是「按 AI 键没反应」。
        #   我们的静音窗口没在跑、它却还是静音 = 卡死了 -> 直接解开。
        try:
            if (winh_mod is not None
                    and winh_mod.voice_capture_muted()
                    and not winh_mod.voice_mute_active()):
                if winh_mod.unstick_voice_capture():
                    _trace("voice capture was stuck muted -> unmuted")
        except Exception:
            pass
        if self._live_seen and not self._ready:
            try:
                muted = bool(winh_mod is not None
                             and winh_mod.voice_capture_muted())
            except Exception:
                muted = False
            if not muted:
                self._ready = True
                # 麦克风真的能收音了 -> 「多久没动静就放弃」的计时从这一刻起算
                #   （之前那几秒是静音窗口 + 面板还没开，不能算用户的沉默）
                self.touch_timeout("mic ready")
        # ⚠️ 他还在说话（麦克风有信号）-> 别把卡片掀了。
        #   Windows 语音输入是**停顿才提交**，长句子中间卡片里一直是空的
        #   -> 只看「有没有新字」会把他切在句子中间。
        try:
            if time.time() < self._speech_until:
                self.touch_timeout("speech")
        except Exception:
            pass
        if self._ready == self._live_state:
            return
        self._live_state = self._ready
        try:
            ui = i18n.ui_lang(self.host.cfg)
            key = "reply.winh_hint" if self._ready else "reply.winh_starting"
            # ⚠️ 不要再用「框里有字就不改提示字」当守卫 —— 2026-11-09 20:00 实测：
            #   语音面板会先往框里打进一个临时字符（那不是用户说的话，会被它自己撤掉），
            #   守卫一挡，提示字就永远停在旧的那句。
            #   占位文字**有字时不显示**，所以直接改是安全的
            #   （实测 setPlaceholderText 不清文档、不动光标）。
            self.editor.setPlaceholderText(i18n.t(ui, key))
            if self._live_seen:
                how = "alive, mic ready" if self._ready else "alive, capture still muted"
            else:
                how = "not alive yet"
            _trace("voice session %s -> card hint = %s" % (how, key))
        except Exception:
            pass

    def _cancel_if_still_dead(self):
        """失焦 250ms 后再判一次：会话只是闪了一下就别把这一轮毙掉。"""
        try:
            if not self.editing:
                return
            try:
                live = bool(winh_mod is not None and winh_mod.voice_ui_active())
            except Exception:
                live = False
            # ⚠️ 瞬时闪断也算「活着」：这一轮见过会话活过 = 面板开着 =
            #   用户马上要开口，这时候把卡片毙掉 = 这一轮白按。
            if live or self._live_seen:
                _trace("card focus out: voice session came back -> keep card")
                QtCore.QTimer.singleShot(120, self._refocus_editor)
                return
            _trace("card focus out -> cancel (voice session still not alive)")
            self._cancel()
        except Exception:
            pass

    def _cancel(self):
        if not self.editing:
            return
        _trace("card cancelled")
        self.editing = False
        try:
            self._live_timer.stop()
        except Exception:
            pass
        self.editor.hide()
        self.editor.clear()
        self.update()
        # Win+H 阶段按 Esc / 点外面 = 「算了」-> 整张卡片收掉，把键盘还回去
        cb = self._winh_cb
        if cb is not None:
            self._winh_cb = None
            try:
                cb()
            except Exception:
                pass
        # 统一出口：卡片收掉 + 焦点还回去（不管有没有 Win+H 回调）
        self.close_card()

    # ---------------------------------------------------------- 兜底计时器
    # ------------------------------------------------ 说话检测（滑动超时的依据）
    def start_speech_watch(self):
        """后台采麦克风电平：他在说话就别把卡片掀了。

        为什么需要（2026-11-09 21:36 实测）：Windows 的语音输入是**停顿才提交**——
        一整句话只在停顿处一次性注入卡片（实测 31 个字一次进来），
        所以长句子中间卡片里一直是空的。只看「有没有新字」判不出「他还在不在说」，
        固定 10 秒超时就切在句子中间 = 用户说的「说话说到一半强退」。

        打不开麦克风就自己退化成「只看新字」（不报错、不影响别的功能）。
        """
        if self._watch_thread is not None:
            return
        if not self._detect_speech:
            return
        self._watch_stop = False
        try:
            import threading
            self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
            self._watch_thread.start()
        except Exception:
            self._watch_thread = None

    def stop_speech_watch(self):
        self._watch_stop = True
        self._watch_thread = None

    def _watch_loop(self):
        """采样线程：只写一个时间戳，不碰 Qt（跨线程碰 GUI 会出事）。"""
        meter = None
        floor = 0.02
        try:
            import mic_level
            meter = mic_level.MicMeter(block_ms=100)
        except Exception as e:
            _trace("speech watch: mic unavailable (%r) -> timeout slides on text only"
                   % (e,))
            return
        try:
            while not self._watch_stop:
                try:
                    rms, _db, _peak = meter.block()
                except Exception:
                    break
                try:
                    if rms <= 0:
                        continue
                    # 噪声底：掉得快、涨得慢
                    if rms < floor:
                        floor = rms
                    else:
                        floor = min(rms, floor * 1.2 + 0.0005)
                    thr = min(max(0.02, floor * 6.0), 0.08)
                    if rms >= thr:
                        self._speech_until = time.time() + 0.4
                except Exception:
                    pass
        finally:
            try:
                meter.close()
            except Exception:
                pass

    def touch_timeout(self, reason=""):
        """「多久没动静就放弃」这个截止时间往后推一个窗口。"""
        try:
            if self._winh_cb is None:
                return False
            now = time.time()
            delay = self._timeout_sec
            if self._timeout_hard_deadline:
                left = self._timeout_hard_deadline - now
                if left <= 0:
                    return False
                delay = min(delay, max(0.2, left))
            self._winh_timer.start(int(delay * 1000))
            return True
        except Exception:
            return False

    def arm_timeout(self, sec, cb, max_sec=None):
        """Win+H 阶段兜底：没人说话也没人确认，卡片不能一直挂着抢键盘。

        ⚠️ 2026-11-09 21:36 修：这里原来是**从按键那刻起算的一次性定时器**，
        用户正在说话也会被掀了 —— 实测 31 个字刚进来，2 秒后卡片就被关，
        语音会话一起被 Esc 掉 =「说话说到一半强退」。
        现在改成**滑动超时**：
          - 每次有识别结果进来 / 麦克风有信号 -> 截止时间往后推；
          - 绝对上限 max_sec 兜底（卡片不会永远挂着）。
        """
        self._winh_cb = cb
        self._timeout_sec = max(1.0, float(sec))
        self._timeout_hard_deadline = (time.time() + float(max_sec)) if max_sec else 0.0
        try:
            self._winh_timer.start(int(self._timeout_sec * 1000))
        except Exception:
            pass
        # 只有 Win+H 阶段才有「麦克风就绪」这回事
        try:
            self._live_timer.start()
        except Exception:
            pass
        self.start_speech_watch()

    def cancel_timeout(self):
        try:
            self._winh_timer.stop()
        except Exception:
            pass
        self._winh_cb = None
        # 所有收尾路径都经过 close_card -> cancel_timeout，
        # 这里停掉麦克风监测线程（不然它会长期占着麦克风）
        self.stop_speech_watch()

    def _winh_timeout(self):
        cb = self._winh_cb
        self._winh_cb = None
        if cb is None:
            return
        _trace("winh timeout: nobody answered, closing the card%s"
               % (" (hit the hard cap)"
                  if (self._timeout_hard_deadline
                      and time.time() >= self._timeout_hard_deadline) else ""))
        try:
            cb()
        except Exception:
            pass

    def _submit(self):
        text = self.editor.toPlainText().strip()
        self.editing = False
        try:
            self._live_timer.stop()
        except Exception:
            pass
        self.editor.hide()
        self.editor.clear()
        self.update()
        if text:
            self.sig_submit.emit(text)
        # ⚠️ 2026-11-09：提交这条路**以前根本没关过语音会话** ——
        #   只有 close_card 会发 Esc，而 submit 不经过 close_card。
        #   结果：用户按回车之后，系统语音输入还按着键盘（用户的字被吃掉），
        #   我们只能干等它自己超时 —— 而那个等待是**阻塞在 GUI 线程上**的，
        #   实测「每次按回车都卡一会」。
        try:
            if winh_mod is not None and winh_mod.dismiss_voice_session():
                _trace("submit: dismissed the live voice session")
        except Exception:
            pass
        # ⚠️ 输入已经结束了 —— 键盘该还给用户。
        # 以前一直挂着焦点，卡片一藏，用户后面敲的字就全进了别处（看着像键盘失灵）。
        self.restore_foreground()

    # ---------------------------------------------------------- 焦点 / 回车把关
    def remember_foreground(self):
        """记住抢焦点之前的前台窗口 —— 用完必须还回去。"""
        try:
            self._prev_fg = winh_mod.foreground_hwnd() if winh_mod is not None else 0
        except Exception:
            self._prev_fg = 0
        self._stole_focus = False

    def mark_focus_stolen(self, ok):
        """标记「焦点确实被我们抢走了」—— 没抢就别还。"""
        self._stole_focus = bool(ok)

    def restore_foreground(self):
        """把键盘焦点还给原来的窗口。

        ⚠️ 抢了不还 = 用户后面敲的字全都进了这个卡片，而卡片此时已经藏起来，
        看起来就像「键盘失灵」。

        ⚠️ 只有**真抢过**才还：覆盖层的 set_state/set_caption 会中途同步一次
        卡片（那时文案还是空的、卡片会先藏一下），那时候我们还没抢焦点，
        要是那时候就把 _prev_fg 用掉，最后反而没人还焦点。
        """
        if not self._stole_focus:
            # 没抢过焦点就别动 _prev_fg —— 它是给「收尾那次还焦点」留的，
            # 这里清掉的话最后就没人还了。
            return
        hwnd = self._prev_fg
        self._prev_fg = 0
        self._stole_focus = False
        if not hwnd:
            return
        # ⚠️⚠️ 2026-11-09：这里原来是个 `while ...: time.sleep(0.1)` 的**阻塞等待**
        #   （最多 2.5 秒），而且跑在 **Qt GUI 线程**上 —— 整个 app 在那 2.5 秒里是死的
        #   （实测：每次按回车都「卡一会」）。
        #   现在改成**定时器轮询**：界面照常满帧，语音会话一死就把焦点还回去。
        self._restore_hwnd = hwnd
        self._restore_deadline = time.time() + 2.5
        try:
            if self._restore_timer is None:
                self._restore_timer = QtCore.QTimer(self)
                self._restore_timer.setInterval(100)
                self._restore_timer.timeout.connect(self._poll_restore)
        except Exception:
            pass
        self._poll_restore()

    def _poll_restore(self):
        """非阻塞地等语音会话收工，然后还焦点。"""
        hwnd = self._restore_hwnd
        if not hwnd:
            self._stop_restore()
            return
        alive = False
        try:
            alive = bool(winh_mod is not None and winh_mod.voice_ui_active())
        except Exception:
            alive = False
        if alive and time.time() < self._restore_deadline:
            return                       # 还在等 —— 但界面不卡
        waited = max(0.0, 2.5 - (self._restore_deadline - time.time()))
        self._stop_restore()
        try:
            ok = winh_mod.restore_foreground(hwnd) if winh_mod is not None else False
            _trace("restore foreground: hwnd=%s ok=%s%s"
                   % (hwnd, ok,
                      " (waited %.1fs for the voice session)" % waited
                      if waited > 0.05 else ""))
        except Exception:
            pass

    def _stop_restore(self):
        self._restore_hwnd = 0
        try:
            if self._restore_timer is not None:
                self._restore_timer.stop()
        except Exception:
            pass

    def _enter_is_user(self):
        """这一下回车是**用户亲手按的**吗？

        真人按的 -> 放行。
        程序注入的 -> 只有**输入法正在组字**时才放行
        （中文输入法提交时按的那一下回车也是注入的，但那是用户的意思）。
        """
        if keyhook_mod.last_key_physical(0x0D):
            return True
        if time.time() < self._ime_until:
            return True
        try:
            return bool(winh_mod.ime_composing()) if winh_mod is not None else False
        except Exception:
            return False

    def _on_text_changed(self):
        """输入框里的字变了 —— 记一笔是谁写进来的。"""
        try:
            n = len(self.editor.toPlainText())
            if n <= 0:
                return          # 清空输入框也会触发，别刷日志
            who = ("keyboard" if keyhook_mod.last_physical_key_recent(0.8)
                   else "voice/other")
            _trace("card text changed by %s: %d chars" % (who, n))
            # 有新字 = 他还在说（或在打字）-> 截止时间往后推
            self.touch_timeout("text")
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        if obj is self.editor:
            if ev.type() == QtCore.QEvent.InputMethod:
                # 输入法在组字/提交 -> 记住这一刻（提交的回车是注入的）
                self._ime_until = time.time() + 1.5
            elif ev.type() == QtCore.QEvent.KeyPress:
                if ev.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
                    if ev.modifiers() & QtCore.Qt.ShiftModifier:
                        return False            # Shift+Enter = 换行
                    if not self._enter_is_user():
                        # Windows 语音输入把识别到的字塞进来之后，
                        # 会自己补一个回车来「提交」。那是程序注入的，
                        # **不是人在确认** —— 吞掉，字留在输入框里。
                        _trace("submit blocked: the Enter was injected, "
                               "not pressed by the user")
                        return True
                    self._submit()
                    return True
                if ev.key() == QtCore.Qt.Key_Escape:
                    self._cancel()
                    return True
            elif ev.type() == QtCore.QEvent.FocusOut:
                # ⚠️ 2026-11-09 修：Windows 语音输入 UI 一打开就把输入框的焦点抢走。
                #   以前这里无条件 _cancel() -> 卡片关掉 -> 焦点还给用户的应用，
                #   可**语音会话还活着**（麦克风坏，它一直等音频）——
                #   用户的应用拿回了焦点，键盘却被系统语音输入按着，
                #   表现就是「别的应用没有反馈」，只能靠人按 Esc 才恢复。
                #   现在：语音会话还活着时，失焦是**预期内的** —— 不取消卡片，
                #   反而把焦点抢回输入框（识别到的字才有地方落）。
                try:
                    live = bool(winh_mod is not None and winh_mod.voice_ui_active())
                except Exception:
                    live = False
                if live:
                    _trace("card focus out (voice session alive) -> keep card, refocus")
                    QtCore.QTimer.singleShot(120, self._refocus_editor)
                    return True
                # ⚠️ 会话状态**会闪**：Windows 的语音面板自己在藏/显之间切时，
                #   音频会话会瞬时 inactive。立刻判死 = 整轮白按 ——
                #   卡片一关，我们会主动 Esc 掉会话，用户的字再也落不进来。
                #   -> 延迟 250ms 复查一次再决定。
                _trace("card focus out -> recheck in 250ms")
                QtCore.QTimer.singleShot(250, self._cancel_if_still_dead)
        return False

    def _refocus_editor(self):
        """把焦点抢回输入框。

        Windows 语音输入 UI 会把焦点从我们的输入框上抢走。不抢回来的话，
        识别到的字就没有地方落 —— 以前是靠「卡片提前取消」来躲开，
        结果语音会话留在后台一直按着键盘（别的应用就没反馈了）。
        """
        try:
            if not self.editing or not self.isVisible():
                return
            try:
                live = bool(winh_mod is not None and winh_mod.voice_ui_active())
            except Exception:
                live = False
            if not live:
                return
            self.editor.setFocus(QtCore.Qt.OtherFocusReason)
            QtCore.QTimer.singleShot(250, self._refocus_editor)
        except Exception:
            pass


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
    conv = context_mod.get_conversation()
    # 「清空上下文」是本地指令：直接把记忆清掉，不经过模型
    if not reply and context_mod.is_clear_command(text):
        conv.clear()
        reply = i18n.t(ui, "ctx.cleared")
    if not reply:
        emit("thinking", i18n.t(ui, "reply.thinking"))
        reply = None
        # 🈳 上下文：前几轮一起发给模型，「把它改短一点」这种指代才接得上
        hist = []
        if context_mod.memory_enabled(cfg):
            try:
                hist = context_mod.normalize_roles(
                    conv.history(context_mod.history_turns(cfg)))
            except Exception:
                hist = []
        # 🈳 助理能力：开了开关、且模型支持工具调用时，让模型自己去调工具干活。
        #    任何一步失败都回落到普通聊天 —— 加了工具不等于只能说工具。
        try:
            reply = agent_mod.run_agent(cfg, llm, text, emit=emit, history=hist)
        except Exception:
            reply = None
        if not reply:
            try:
                msgs = context_mod.normalize_roles(
                    list(hist) + [{"role": "user", "content": text}])
                reply = llm.chat(msgs, system=context_mod.compose_system(cfg))
            except Exception as e:
                reply = i18n.t(ui, "reply.model_down") % str(e)[:70]
    # 记这一轮（问 + 答）
    try:
        conv.add_turn(text, reply)
    except Exception:
        pass
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


def _send_winh():
    """真的把 Win+H 按出去 —— 单独一层是为了能在日志里留下「发没发」的证据。"""
    ok = False
    try:
        ok = bool(winh_mod.send_hotkey()) if winh_mod is not None else False
    except Exception as e:
        _trace("_send_winh raised: %r" % (e,))
    _trace("_send_winh done, sent=%s" % ok)
    return ok


def _begin_winh(overlay, card):
    """Win+H 模式：弹卡片 -> 输入框拿焦点 -> 发 Win+H -> 等 Windows 往里打字。

    为什么不自己开麦：这台机器的采集链路在驱动层就断了（见 win_h.py）。
    Win+H 把采集和识别都交给 Windows，本项目只负责把输入框摆在前面。

    做成**模块级函数**是为了能单测 —— main() 里的闭包没法在测试里碰到。
    """
    ui = i18n.ui_lang(overlay.cfg)
    # 一开始是「正在打开」；面板真的就绪后，卡片上的字会自己变成「麦克风已就绪」
    hint = i18n.t(ui, "reply.winh_starting")
    # 抢焦点之前先记住原来是谁在前台 —— 用完要还回去
    card.remember_foreground()
    overlay.set_ui("listening", hint)
    _trace("_begin_winh: set_ui done")
    card.start_edit(placeholder=hint)
    _trace("_begin_winh: start_edit done, card visible=%s" % card.isVisible())
    if winh_mod is not None:
        ok = winh_mod.force_foreground(card)
        card.mark_focus_stolen(ok)
        card.editor.setFocus()
        _trace("_begin_winh: force_foreground=%s, focus queued for Win+H" % ok)
        # 给 Windows 一点时间把焦点落实 —— 发早了焦点还在别的程序手里
        stt_cfg = (overlay.cfg.get("stt") or {})
        # 兜底：没人说话也没人确认 -> 卡片自己收掉，把键盘还回去
        # ⚠️ 2026-11-09 修：以前这里是写死的 45 秒，而且**根本不看设置面板上的
        #   「听写超时」**（stt.timeout_sec）—— 那个旋钮只在离线听写那条路上生效。
        #   结果：Win+H 模式下没人说话时，语音会话能一直活着（麦克风坏，它一直
        #   等音频），键盘一直被系统按着，别的应用全部没反馈。
        #   现在：**统一听设置面板上那个「听写超时」**（默认 10 秒），
        #   到点强制收卡片 + 强制关掉语音会话，键盘立刻还给用户。
        winh_timeout = float(stt_cfg.get("timeout_sec", 10) or 10)

        def _give_up():
            try:
                overlay.set_ui("idle", "")
            except Exception:
                pass

        # 滑动超时的绝对上限：卡片最多活这么久（与「隐藏语音面板」的时长一致，
        #   超过它 Windows 面板就会自己冒出来）
        hide_max = float(stt_cfg.get("hide_max_seconds", 120.0) or 120.0)
        card.arm_timeout(winh_timeout, _give_up, max_sec=hide_max)
        # 开始录音那声「叮」：提前把语音输入宿主那个音频会话压掉（只压这一小会儿）
        # 注意：必须排在发 Win+H **之前** —— 提示音是 Windows 收到按键就播的
        if stt_cfg.get("mute_start_sound", True):
            winh_mod.mute_voice_start_sound(
                float(stt_cfg.get("mute_seconds", 1.8) or 1.8),
                on_change=lambda info: _trace("mute_voice_start_sound: " + info))
        QtCore.QTimer.singleShot(400, _send_winh)
        # 语音输入面板 / 提示卡：Windows 自己的 UI，没有官方开关，只能自己藏
        if stt_cfg.get("hide_panel", True):
            winh_mod.suppress_voice_ui(
                max_seconds=float(stt_cfg.get("hide_max_seconds", 120.0) or 120.0),
                on_hide=lambda info: _trace("suppress_voice_ui: hid " + info))
        _trace("_begin_winh: Win+H singleShot armed")


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


def _redirect_stdout_if_frozen():
    """打包成 exe 之后没有控制台 —— stdout/stderr 得自己落到日志文件。

    不落盘的话，pythonw 下 traceback 直接丢掉，出了错什么都看不见。

    ⚠️ 必须**行缓冲**（buffering=1）：默认块缓冲的话常驻进程不退出，
       缓冲区永远不刷，日志文件一直是空的 —— 看着像「零异常」，其实什么都没记上。
    """
    if pathmod is None or not pathmod.is_frozen():
        return None
    try:
        d = os.path.join(pathmod.data_dir(), "logs")
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, "siri_orb.log"), "a", buffering=1, encoding="utf-8")
        sys.stdout = f
        sys.stderr = f
        return f
    except Exception:
        return None


def _notify(title, text):
    """弹一个系统级提示框。

    托盘常驻、没有控制台 —— 想让用户看见的事，不能只 print 到日志里。
    """
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        pass


def _resolve_hotkey(preferred):
    """热键被别的程序占了怎么办 —— 自动顺延，并把实际生效的键告诉用户。

    以前 RegisterHotKey 失败只在 stdout 打一行，用户完全无感知，
    表现就是「按了没反应」。顺延顺序：原值 -> ctrl+alt+shift+space
    -> ctrl+shift+space -> F9。
    """
    cands = [preferred] + [c for c in ("ctrl+alt+shift+space", "ctrl+shift+space", "F9")
                           if c != preferred]
    for hk in cands:
        if not hk:
            continue
        try:
            mods, vk = keyhook_mod.parse_hotkey(hk)
        except Exception:
            continue
        if not vk:
            continue
        ok = False
        try:
            ok = bool(keyhook_mod.user32.RegisterHotKey(
                None, 0xBEEF, mods | keyhook_mod.MOD_NOREPEAT, vk))
        except Exception:
            ok = False
        try:
            ctypes.windll.user32.UnregisterHotKey(0, 0xBEEF)
        except Exception:
            pass
        if ok:
            if hk != preferred:
                return hk, ("原热键 %s 被别的程序占用了，已自动改用 %s\n"
                            "（小米语音键不受影响，照常可用）" % (preferred, hk))
            return hk, None
    return preferred, None


def main():
    # 打包后同一个 exe 也能开设置面板（--settings）
    if "--settings" in sys.argv:
        import settings as settings_mod
        sys.exit(settings_mod.main())
    # ---- 单实例互斥（要早于 QApplication）----
    # 以前没有：双开就是两个实例同时装低级键盘钩子、同时抢热键、同时抢焦点，
    # 一个 Win+H 的输入被两边消费 —— 2026-11-08 日志里有 8 组「6~7 秒内连开两次」。
    _mutex = None
    try:
        _k32 = ctypes.windll.kernel32
        # ⚠️ 测试实例用环境变量换一个互斥量名：
        #    常驻实例不动，我们并行跑一个测试实例去量启动耗时（不碰他的实例）。
        _tag = (os.environ.get("SIRI_OS_INSTANCE_TAG") or "").strip()
        _suffix = ("." + _tag) if _tag else ""
        _mutex = _k32.CreateMutexW(None, False, "Global\\SiriOS.Singleton" + _suffix)
        if not _mutex:
            _mutex = _k32.CreateMutexW(None, False, "Local\\SiriOS.Singleton" + _suffix)
        _already = (_k32.GetLastError() == 183)   # ERROR_ALREADY_EXISTS
    except Exception:
        _mutex = None
        _already = False
    if _already:
        _notify("Siri-OS", "已经在运行了（看右下角托盘图标）。")
        return 0
    _redirect_stdout_if_frozen()
    # ⚠️ 启动第一件事：把「这次读的是哪份配置」写进日志。
    #    「重启后配置被重置」这类问题的第一手证据就是这个路径：
    #    源码模式读脚本目录、打包模式读 exe 同级目录 —— 指错了就是另一份文件，
    #    看着像「配置被重置」，其实是两份配置在互相覆盖。
    _trace("startup: config=%s exists=%s frozen=%s" % (
        CONFIG_PATH, os.path.exists(CONFIG_PATH),
        bool(getattr(sys, "frozen", False))))
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
    # ⚠️ 预热渲染路径：第一次 render() 要 53ms（见 Overlay.warmup 的说明）。
    #    不预热的话，这 53ms 会正好砸在用户第一次按 AI 键的那一帧上。
    overlay.warmup()

    # 大致位置：**后台**预热 —— 定位要 2~4 秒，不能让第一句话干等。
    # 位置本身由 locate 模块管（缓存 + 定期刷新），这里只把活配置喂给它。
    try:
        import locate as locate_mod
        locate_mod.set_cfg_provider(lambda: overlay.cfg)
        locate_mod.start_warmup()
    except Exception as e:
        _trace("locate warmup failed: %r" % (e,))

    # 截图工具用得上：截之前先把覆盖层藏起来，否则截出来的是自己的光晕
    def _hide_overlay(hide):
        try:
            if hide:
                overlay.hide()
            else:
                overlay.show()
        except Exception:
            pass

    try:
        tools_mod.set_hook("hide_overlay", _hide_overlay)
    except Exception:
        pass

    # 天气工具查完 -> 把结构化数据推上屏幕（Live Activity 式面板）
    # ⚠️ 必须走信号：工具跑在 agent 的工作线程里，直接调 GUI 方法
    #    （set_weather -> card.set_ui -> update）会被 Qt 丢掉重绘，
    #    卡片不重绘 = 面板永远不上屏（2026-11-08 实测：面板死活不出来）。
    try:
        overlay.sig_weather.connect(overlay.set_weather)
        tools_mod.set_hook("weather_panel", lambda p: overlay.sig_weather.emit(p))
    except Exception as e:
        _trace("weather hook failed: %r" % (e,))

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
        _trace("submit: %s" % text[:80].replace("\n", " "))
        try:
            overlay.last_question = text      # 天气面板的标题就是这一句
        except Exception:
            pass
        try:
            card.cancel_timeout()      # 已经确认了，别再兜底关卡片
        except Exception:
            pass
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

    # ⚠️ 必须**显式** QueuedConnection：keyhook 回调跑在普通 Python 线程上，
    # PyQt5 对这种线程发信号时 AutoConnection 可能判成「同线程」-> 直接连接 ->
    # 槽函数在 keyhook 线程里跑：card.show() 跨线程静默失效（卡片不出现），
    # QTimer.singleShot 在没有事件循环的线程里**永不触发**（Win+H 发不出去）。
    overlay.sig_trigger.connect(overlay.do_winh, QtCore.Qt.QueuedConnection)

    def trigger():
        nonlocal voice_thread
        if voice_thread is not None and voice_thread.isRunning():
            return
        overlay.mark_press()
        stt = (overlay.cfg.get("stt") or {})
        if (stt.get("mode") or "winh").lower() == "winh" and winh_mod is not None:
            import threading
            _trace("trigger: emitting sig_trigger  thread=%s" % threading.current_thread().name)
            # ⚠️ 这里跑在 keyhook 线程上，**绝不能直接碰 GUI** ——
            # 跨线程 show()/setFocus() 静默失效，QTimer.singleShot 永不触发。
            # 用信号把活搬回 GUI 线程。
            overlay.sig_trigger.emit()
            return
        # 同上：语音线程用最新配置（听写超时/播报音色/语速改了立刻生效）
        voice_thread = VoiceThread(overlay.cfg, llm=llm_shared)
        voice_thread.sig_ui.connect(overlay.set_ui)
        voice_thread.sig_level.connect(overlay.set_level)
        voice_thread.start()

    def press_start():
        """按住小米键：先亮按钮 + 画进度环当反馈，但**不开卡片、不扩散**。

        等按住满阈值才真的触发（见 keyhook.HoldGesture）。
        """
        overlay.mark_hold_start(
            max(0.05, float(overlay.cfg.get("hold_trigger_ms", 500) or 500) / 1000.0))

    def press_end(held):
        """短按（没到阈值就松手）：什么都不触发，只把按住时的余晖收掉。"""
        overlay.mark_hold_end()

    if demo:
        run_demo(overlay)
    else:
        hotkey, hotkey_note = _resolve_hotkey(cfg.get("hotkey", "ctrl+alt+space"))
        if hotkey_note:
            # 实际生效的热键要写回配置 —— 否则设置页显示的还是那个被占用的键
            try:
                cfg["hotkey"] = hotkey
                cfgstore.save(cfg)
            except Exception:
                pass
            _trace("hotkey fallback: %s" % hotkey_note.replace("\n", " "))
            _notify("Siri-OS 热键", hotkey_note)
        hook = KeyHook(
            on_hotkey=trigger,
            on_match=lambda vk, sc: trigger(),
            hotkey=hotkey,
            scan_code=cfg.get("xiaomi_scan_code"),
            vk=cfg.get("xiaomi_vk"),
            log_path=KEY_LOG,
            log_all=bool(cfg.get("log_all_keys", True)),
            swallow=bool(cfg.get("swallow_matched_key", True)),
            # 长按手势：阈值**每次按下时**现取 —— 设置面板改完立刻生效，不用重启
            hold_sec=lambda: max(0.0, float(overlay.cfg.get("hold_trigger_ms", 500) or 0)) / 1000.0,
            on_press_start=press_start,
            on_short_press=press_end)
        hook.start()
        time.sleep(0.4)
        if hook.error:
            print("[siri_orb] hook error: %s" % hook.error)
        if hook.hotkey_error:
            print("[siri_orb] hotkey error: %s" % hook.hotkey_error)
            _trace("hotkey error: %s" % hook.hotkey_error)
            # 静默失败 = 用户「按了没反应」还不知道为什么
            _notify("Siri-OS 热键", "热键注册失败：%s\n"
                    "可以去设置页换一个别的组合。" % hook.hotkey_error)
        print("[siri_orb] key log: %s" % KEY_LOG)

    def open_settings():
        """拉起设置面板（独立进程 —— 助手没在跑也能打开改配置）。"""
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if pathmod is not None and pathmod.is_frozen():
                # 打包后还是这个 exe，加 --settings
                subprocess.Popen([sys.executable, "--settings"],
                                 cwd=pathmod.data_dir(), creationflags=flags)
            else:
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
        # 托盘图标是打包后唯一的入口 —— 图标为空的话 Windows 根本不会显示它，
        # 打包后没有控制台，只能靠这行日志判断。
        print("[siri_orb] tray icon ready (null=%s)" % tray.icon().isNull())
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
