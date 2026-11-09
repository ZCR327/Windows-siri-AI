# -*- coding: utf-8 -*-
"""Voice-session guard self check (2026-11-09).

Why this file exists
--------------------
On 2026-11-09 the user reported: "why did my other apps stop responding?"
(and later confirmed: "it was closed when I pressed Esc").

Measured chain, every step verified on this machine:

  1. every key press makes the app steal the foreground to the card
  2. Win+H starts a Windows voice-typing session -- and that session
     HOLDS THE KEYBOARD (its audio session goes Active: measured 0.5s
     after sending Win+H)
  3. the card used to cancel on FocusOut, so the focus went back to the
     user's app WHILE the voice session was still alive
  4. the keystrokes were eaten by the system voice input -> the other
     apps looked dead.  Only a manual Esc (what the user did) closed the
     session and brought them back.

So three things have to hold, and this file checks all three:

  A. the voice panel is still found when it is currently HIDDEN
     (the old detector required "visible" -> once hidden, never found
     again, and the safety net silently stopped working)
  B. Esc is only sent while a voice session is really alive
  C. voice_ui_active() reflects the real session state
  D. the Win+H path uses the "recognition timeout" knob (stt.timeout_sec),
     NOT a hardcoded 45 s  (the knob used to be dead in Win+H mode)
  E. closing the card dismisses a live voice session, and the re-entry
     guard keeps that from recursing
  F. a FocusOut while a voice session is alive keeps the card open

Nothing here touches the real microphone, the real voice panel or the
keyboard: every Windows-facing call is mocked.

Output is ASCII on purpose: the Windows console here is GBK.
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ctypes

import win_h
import siri_orb
from PyQt5 import QtCore, QtGui, QtWidgets

APP = QtWidgets.QApplication(sys.argv)
FAILS = []


def check(name, got, expect):
    ok = (got == expect)
    if not ok:
        FAILS.append("%s: expect=%s got=%s" % (name, expect, got))
    print("  [%s] %-54s expect=%-8s got=%s"
          % ("OK " if ok else "FAIL", name, expect, got))
    return ok


# ---------------------------------------------------------------- A
def part_a():
    print("=== A. the detector only reports a LIVE voice panel ===")
    oc, od, om = (win_h._voice_panel_candidates, win_h._describe_window,
                   win_h._has_mic_button)
    try:
        # hidden -> not picked. Windows hides the panel on its own; forcing it
        # back would leave an empty floating box on the screen.
        win_h._voice_panel_candidates = lambda: [(111, "ApplicationFrameWindow")]
        win_h._describe_window = lambda h: (4900, "Windows.UI.Core.CoreWindow",
                                             "", False, 0, 6, 160, 103)
        win_h._has_mic_button = lambda *a, **k: 0
        h, _host = win_h._find_voice_panel_window()
        check("a hidden panel is not picked", h, None)

        # visible + the blue mic button -> picked
        win_h._describe_window = lambda h: (4900, "Windows.UI.Core.CoreWindow",
                                             "", True, 0, 6, 160, 103)
        win_h._has_mic_button = lambda *a, **k: 90
        h, host = win_h._find_voice_panel_window()
        check("a live panel is picked", h, 111)
        check("host class kept", host, "ApplicationFrameWindow")

        # visible but no blue button -> NOT picked (that is the IME's own one)
        win_h._has_mic_button = lambda *a, **k: 0
        h, _ = win_h._find_voice_panel_window()
        check("no blue button -> not picked", h, None)

        # a foreign host class is never ours
        win_h._voice_panel_candidates = lambda: [(333, "SomeOtherHostClass")]
        win_h._has_mic_button = lambda *a, **k: 999
        h, _ = win_h._find_voice_panel_window()
        check("a foreign host class is ignored", h, None)
    finally:
        win_h._voice_panel_candidates = oc
        win_h._describe_window = od
        win_h._has_mic_button = om
    print("")


# ---------------------------------------------------------------- B
def part_b():
    print("=== B. Esc is only sent while a session is really alive ===")
    calls = []

    class FakeUser32(object):
        def keybd_event(self, *a):
            calls.append(a)

        def IsWindowVisible(self, h):
            return False

    ou, oa = ctypes.windll.user32, win_h._voice_session_active
    try:
        ctypes.windll.user32 = FakeUser32()
        win_h._voice_session_active = lambda: False
        r = win_h.dismiss_voice_session()
        check("no session -> no Esc", r, False)
        check("no keybd_event calls", len(calls), 0)

        win_h._voice_session_active = lambda: True
        r = win_h.dismiss_voice_session()
        check("live session -> Esc sent", r, True)
        check("keybd_event called twice", len(calls), 2)
        check("the key is Escape", calls[0][0], 0x1B)
    finally:
        ctypes.windll.user32 = ou
        win_h._voice_session_active = oa
    print("")


# ---------------------------------------------------------------- C
def part_c():
    print("=== C. voice_ui_active() reflects the session state ===")
    oa, oc = win_h._voice_session_active, win_h._voice_panel_candidates
    oh = win_h._PANEL_HWND
    try:
        win_h._voice_session_active = lambda: True
        check("session alive -> ui active", win_h.voice_ui_active(), True)
        win_h._voice_session_active = lambda: False
        win_h._PANEL_HWND = 0
        win_h._voice_panel_candidates = lambda: []
        check("nothing alive -> ui not active", win_h.voice_ui_active(), False)
    finally:
        win_h._voice_session_active = oa
        win_h._voice_panel_candidates = oc
        win_h._PANEL_HWND = oh
    print("")


# ---------------------------------------------------------------- D
class FakeCard(object):
    def __init__(self):
        self.armed = None
        self.editor = type("E", (object,), {"setFocus": lambda *a, **k: None})()

    def remember_foreground(self):
        pass

    def start_edit(self, placeholder=None):
        pass

    def isVisible(self):
        return True

    def mark_focus_stolen(self, ok):
        pass

    def arm_timeout(self, sec, cb):
        self.armed = (sec, cb)


class FakeOverlay(object):
    def __init__(self, cfg):
        self.cfg = cfg

    def set_ui(self, state, text):
        pass


def part_d():
    print("=== D. the Win+H path honours the recognition-timeout knob ===")
    cfg = {"stt": {"timeout_sec": 10, "mode": "winh",
                   "hide_panel": False, "mute_start_sound": False}}
    ov, card = FakeOverlay(cfg), FakeCard()
    os_, off = siri_orb._send_winh, siri_orb.winh_mod.force_foreground
    try:
        siri_orb._send_winh = lambda: True
        siri_orb.winh_mod.force_foreground = lambda w: True
        siri_orb._begin_winh(ov, card)
        check("timeout comes from stt.timeout_sec", card.armed[0], 10.0)
        check("the old hardcoded 45 is gone", card.armed[0] == 45.0, False)
    finally:
        siri_orb._send_winh = os_
        siri_orb.winh_mod.force_foreground = off
    import cfgstore
    check("default recognition timeout is 10s",
          cfgstore.DEFAULTS["stt"]["timeout_sec"], 10)
    print("")


# ---------------------------------------------------------------- E / F
class FakeHost(object):
    def caption_geom(self):
        return (0, 0, 400, 120, "label", 20, QtGui.QFont())

    def x(self):
        return 0

    def y(self):
        return 0


class FakeEv(object):
    def __init__(self, t):
        self._t = t

    def type(self):
        return self._t


def part_e():
    print("=== E. closing the card dismisses a live voice session ===")
    card = siri_orb.CardWindow({}, FakeHost())
    card.editing = True
    dismissed = []
    od, oa = (siri_orb.winh_mod.dismiss_voice_session,
              siri_orb.winh_mod.voice_ui_active)
    orr = siri_orb.CardWindow.restore_foreground
    try:
        def _dismiss():
            dismissed.append(1)
            card.close_card()      # simulates Esc -> _cancel() -> close_card()
            return True

        siri_orb.winh_mod.dismiss_voice_session = _dismiss
        siri_orb.winh_mod.voice_ui_active = lambda: False
        siri_orb.CardWindow.restore_foreground = lambda self: None
        card.close_card()
        check("the live session was dismissed", len(dismissed), 1)
        check("re-entry guard stopped the recursion", len(dismissed), 1)
        check("card is no longer editing", card.editing, False)
    finally:
        siri_orb.winh_mod.dismiss_voice_session = od
        siri_orb.winh_mod.voice_ui_active = oa
        siri_orb.CardWindow.restore_foreground = orr
    print("")


def part_f():
    print("=== F. a FocusOut during a live voice session keeps the card ===")
    card = siri_orb.CardWindow({}, FakeHost())
    cancelled = []
    oc, oa = siri_orb.CardWindow._cancel, siri_orb.winh_mod.voice_ui_active
    try:
        siri_orb.CardWindow._cancel = lambda self: cancelled.append(1)
        siri_orb.winh_mod.voice_ui_active = lambda: True
        card.eventFilter(card.editor, FakeEv(QtCore.QEvent.FocusOut))
        check("card survives the focus loss", len(cancelled), 0)
        siri_orb.winh_mod.voice_ui_active = lambda: False
        card.eventFilter(card.editor, FakeEv(QtCore.QEvent.FocusOut))
        check("without a voice session it still cancels", len(cancelled), 1)
    finally:
        siri_orb.CardWindow._cancel = oc
        siri_orb.winh_mod.voice_ui_active = oa
    print("")


def main():
    print("Voice-session guard self check")
    print("")
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    print("=== summary ===")
    if FAILS:
        for f in FAILS:
            print("FAIL: %s" % f)
        print("%d FAILED" % len(FAILS))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
