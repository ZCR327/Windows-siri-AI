# -*- coding: utf-8 -*-
"""天气面板上屏自检：面板数据必须**走 Qt 信号回主线程**。

为什么要有这个检查（2026-11-08 实测踩的坑）：
  天气工具跑在 agent 的**工作线程**里。以前钩子直接调 overlay.set_weather()，
  那是 GUI 操作 —— setGeometry / show / raise_ 都是**直接调用**，不是排队的事件，
  从工作线程调等于跟 GUI 线程抢窗口状态（重绘事件能不能投递全看运气）。
  现在统一走 sig_weather.emit()：信号是跨线程安全的，会排队到 GUI 线程执行。

要验的三件事：
  1. 工作线程 emit 之后，面板数据到位、标题是用户原话、卡片换成天气布局宽度；
  2. 一轮结束（idle）时面板**不立刻消失**（照 iOS Live Activity：答完还在），
     而是留 weather_linger_sec 再收；
  3. linger 到了以后面板收掉、卡片跟着走。

用法：
    python tools\\siri_orb\\weather_panel_check.py
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402
import cfgstore  # noqa: E402
import siri_orb  # noqa: E402

PAYLOAD = {"place": "\u4e0a\u6d77\u5e02\u6d66\u4e1c\u65b0\u533a",
           "city": "\u4e0a\u6d77\u5e02\u6d66\u4e1c\u65b0\u533a",
           "temp": 16.3, "feels": 16.6, "hi": 18.4, "lo": 14.3, "code": 61,
           "icon": "rain", "text": "\u5c0f\u96e8", "text_en": "Light rain",
           "pop": 100, "updated": "20:15", "day": "today"}
QUESTION = "\u4eca\u5929\u5929\u6c14\u600e\u4e48\u6837"

FAIL = []
OK = []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                       (" -- " + detail) if detail else ""))


class CountingCard(siri_orb.CardWindow):
    """数 paintEvent 真的被投递了几次（屏幕上真的重绘 = 这个数涨）。"""

    def __init__(self, *a, **kw):
        self.paints = 0
        siri_orb.CardWindow.__init__(self, *a, **kw)

    def paintEvent(self, ev):
        self.paints += 1
        siri_orb.CardWindow.paintEvent(self, ev)


def spin(ms=400):
    """让事件循环转一会儿（重绘事件就是在这段时间里投递的）。"""
    end = time.time() + ms / 1000.0
    while time.time() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)


def main():
    app = QtWidgets.QApplication(sys.argv)
    cfg = dict(cfgstore.DEFAULTS)
    overlay = siri_orb.Overlay(cfg)
    overlay.resize(1920, 1080)
    card = CountingCard(cfg, overlay)
    overlay.set_card(card)
    # 真实链路里 _ask() 会先把用户原话记下来（面板标题就用它）
    overlay.last_question = QUESTION
    overlay.set_ui("thinking", QUESTION)
    spin(300)
    base = card.paints

    # ---- 1. 工作线程 emit（现在的做法）
    overlay.sig_weather.connect(overlay.set_weather)

    def via_signal():
        overlay.sig_weather.emit(PAYLOAD)

    th = threading.Thread(target=via_signal)
    th.start()
    th.join()
    spin(700)
    check("\u9762\u677f\u6570\u636e\u5230\u4f4d", bool(overlay.weather),
          repr(overlay.weather)[:70])
    check("\u6807\u9898\u662f\u7528\u6237\u539f\u8bdd", overlay.caption == QUESTION,
          overlay.caption)
    want_w = min(int(cfg.get("caption_max_width", 900) or 900), 860) + 2 * card.PAD
    check("\u5361\u7247\u6362\u6210\u5929\u6c14\u5e03\u5c40\u5bbd\u5ea6",
          abs(card.width() - want_w) <= 2,
          "card.width=%d want=%d" % (card.width(), want_w))
    check("\u5361\u7247\u771f\u7684\u91cd\u7ed8\u4e86", card.paints - base > 0,
          "paints=%d" % (card.paints - base))

    # ---- 2. 一轮结束（idle）：面板别立刻消失
    overlay.set_state("idle")
    spin(200)
    check("\u4e00\u8f6e\u7ed3\u675f\u540e\u9762\u677f\u4ecd\u5728", bool(overlay.weather),
          "linger_until=%.1f" % overlay._weather_until)
    check("\u9762\u677f\u7559\u4e0b\u6765\u7684\u65f6\u957f\u6309\u914d\u7f6e\u7b97",
          overlay._weather_until > time.time(),
          "linger=%.1fs" % (overlay._weather_until - time.time()))

    # ---- 3. linger 到了：面板收掉、卡片跟着走
    overlay._weather_until = time.time() - 0.01
    spin(300)
    check("\u5230\u65f6\u9762\u677f\u6536\u6389", not overlay.weather,
          repr(overlay.weather)[:40])
    check("\u5230\u65f6\u5361\u7247\u4e5f\u8ddf\u7740\u8d70", card.caption == "",
          repr(card.caption))

    print("")
    print("%d checks, %d FAIL" % (len(OK) + len(FAIL), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
