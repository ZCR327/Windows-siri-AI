# -*- coding: utf-8 -*-
"""渲染缓存路径回归自检。

背景：为了压「扩散时掉帧」，底光 fall/ring/hot 被改成「按电平分档缓存」。
结果跑马灯分支（wave=None、marquee_gain>0）在**缓存命中**时引用了
只在「缓存未命中」分支里赋值的局部名 ring / hot ->
第二帧起 UnboundLocalError，听写/说话状态直接崩。

这类 bug 只在「同一个 renderer 连渲两帧」时才出现：
每次新建 renderer 都是冷缓存，反而看不出问题。

这个脚本只做一件事：同一个 renderer 连渲两帧（冷/热），
要求 ① 不抛异常 ② 两帧逐像素一致（<=1 LSB）。
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np

import siri_orb

LSB = 1.0 / 255.0

CASES = [
    # (t, intensity, mode, wave, marquee_gain, level)
    (0.0, 1.0, 'listening', None, 1.0, 0.0),
    (3.7, 1.0, 'listening', {'radius': 900.0, 'width': 46.0}, 0.0, 0.0),
    (3.7, 1.0, 'listening', {'radius': 300.0, 'width': 46.0}, 0.0, 0.0),
    (12.5, 0.82, 'thinking', None, 1.0, 0.0),
    (5.0, 1.0, 'speaking', None, 1.0, 0.55),
    (5.0, 1.0, 'listening', None, 1.0, 0.0),
]

fails = []
checked = 0


def check(cond, msg):
    global checked
    checked += 1
    if not cond:
        fails.append(msg)
        print('  FAIL  ' + msg)
    else:
        print('  ok    ' + msg)


print('=== 1) 冷/热两帧必须逐像素一致，且不抛异常 ===')
for i, c in enumerate(CASES):
    gr = siri_orb.GlowRenderer(1440, 900)
    try:
        cold = gr.render(*c)
    except Exception as e:
        check(False, 'case %d 冷帧抛异常: %r' % (i, e))
        continue
    try:
        warm = gr.render(*c)
    except Exception as e:
        check(False, 'case %d 热帧抛异常: %r' % (i, e))
        continue
    d = float(np.max(np.abs(cold - warm)))
    check(d <= LSB, 'case %d 冷/热一致 (max|diff|=%.6f, 1LSB=%.6f)' % (i, d, LSB))

print('=== 2) 电平变化必须重新算底光（缓存不能串档）===')
gr = siri_orb.GlowRenderer(1440, 900)
gr.render(5.0, 1.0, 'listening', None, 1.0, 0.0)
gr.render(5.0, 1.0, 'listening', None, 1.0, 0.55)
gr._base_key = None
a = gr.render(5.0, 1.0, 'listening', None, 1.0, 0.55)
gr2 = siri_orb.GlowRenderer(1440, 900)
b = gr2.render(5.0, 1.0, 'listening', None, 1.0, 0.55)
d = float(np.max(np.abs(a - b)))
check(d <= LSB, '换电平后与全新 renderer 一致 (max|diff|=%.6f)' % d)

print('=== 3) u8 与浮点两条输出路径一致 ===')
# 注意量纲：_blur_u8 吐 uint8(0~255)，_blur 吐 float32(0~1) —— 比之前要归一化
gr = siri_orb.GlowRenderer(1440, 900)
f32 = gr.render(3.7, 1.0, 'listening', {'radius': 900.0, 'width': 46.0}, 0.0, 0.0)
u8 = gr.render(3.7, 1.0, 'listening', {'radius': 900.0, 'width': 46.0}, 0.0, 0.0, u8=True)
d = float(np.max(np.abs(f32.astype(np.float32) - u8.astype(np.float32) / 255.0)))
check(d <= LSB, 'u8 与 float32 一致 (max|diff|=%.6f, 1LSB=%.6f)' % (d, LSB))

print('=== 4) 热帧不应比强制重算慢（缓存确实在起作用）===')
gr = siri_orb.GlowRenderer(1440, 900)
gr.render(*CASES[0])
t0 = time.perf_counter()
for _ in range(20):
    gr.render(*CASES[0])
warm_ms = (time.perf_counter() - t0) / 20 * 1000
gr2 = siri_orb.GlowRenderer(1440, 900)
t0 = time.perf_counter()
for _ in range(20):
    gr2._base_key = None
    gr2.render(*CASES[0])
cold_ms = (time.perf_counter() - t0) / 20 * 1000
print('  热 %.2f ms / 强制重算 %.2f ms' % (warm_ms, cold_ms))
check(warm_ms <= cold_ms, '热帧不慢于强制重算')

print()
print('%d checks, %d FAIL' % (checked, len(fails)))
sys.exit(1 if fails else 0)
