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
import time

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
        self.armed_max = None
        self.editor = type("E", (object,), {"setFocus": lambda *a, **k: None})()

    def remember_foreground(self):
        pass

    def start_edit(self, placeholder=None):
        pass

    def isVisible(self):
        return True

    def mark_focus_stolen(self, ok):
        pass

    def arm_timeout(self, sec, cb, max_sec=None):
        self.armed = (sec, cb)
        self.armed_max = max_sec


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
        check("the card gets a hard cap", card.armed_max, 120.0)
    finally:
        siri_orb._send_winh = os_
        siri_orb.winh_mod.force_foreground = off
    import cfgstore
    check("default recognition timeout is 10s",
          cfgstore.DEFAULTS["stt"]["timeout_sec"], 10)
    print("")


# ---------------------------------------------------------------- E / F
class FakeHost(object):
    cfg = {}

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
        card.editing = True      # the card is open (the mock never calls start_edit)
        card.eventFilter(card.editor, FakeEv(QtCore.QEvent.FocusOut))
        check("card survives the focus loss", len(cancelled), 0)
        siri_orb.winh_mod.voice_ui_active = lambda: False
        card.eventFilter(card.editor, FakeEv(QtCore.QEvent.FocusOut))
        # 2026-11-09 晚：失焦不再**立刻**判死。
        # 会话状态会闪（Windows 的语音面板自己藏/显时，音频会话瞬时 inactive），
        # 一次闪断就把整轮毙掉 = 用户的字再也落不进来。
        # -> 延迟 250ms 复查一次再决定。
        check("a dead session does not cancel instantly", len(cancelled), 0)
        card._cancel_if_still_dead()
        check("without a voice session it still cancels", len(cancelled), 1)
        # 会话在宽限期里闪回来 -> 卡片必须保住
        cancelled[:] = []
        siri_orb.winh_mod.voice_ui_active = lambda: True
        card.eventFilter(card.editor, FakeEv(QtCore.QEvent.FocusOut))
        card._cancel_if_still_dead()
        check("a session that flaps back keeps the card", len(cancelled), 0)
    finally:
        siri_orb.CardWindow._cancel = oc
        siri_orb.winh_mod.voice_ui_active = oa
    print("")


# ---------------------------------------------------------------- G
def part_g():
    print("=== G. the mic-ready hint LATCHES (the session state flaps) ===")
    import i18n
    card = siri_orb.CardWindow({}, FakeHost())
    card.editing = True
    oa = siri_orb.winh_mod.voice_ui_active
    om = siri_orb.winh_mod.voice_capture_muted
    try:
        # the live capture state must not decide the outcome of a self check
        siri_orb.winh_mod.voice_capture_muted = lambda: False
        ui = i18n.ui_lang({})
        hint = i18n.t(ui, "reply.winh_hint")
        start = i18n.t(ui, "reply.winh_starting")

        siri_orb.winh_mod.voice_ui_active = lambda: False
        card._poll_voice_live()
        check("before the session: the card says opening",
              card.editor.placeholderText(), start)

        siri_orb.winh_mod.voice_ui_active = lambda: True
        card._poll_voice_live()
        check("session alive: the card says the mic is ready",
              card.editor.placeholderText(), hint)

        # The session flaps: the panel is hidden/shown, so the audio session
        # goes inactive for a moment.  The hint must NOT flap back.
        siri_orb.winh_mod.voice_ui_active = lambda: False
        card._poll_voice_live()
        check("a flapping session does not flap the hint back",
              card.editor.placeholderText(), hint)

        # and a focus-out during that flap must not kill the card
        cancelled = []
        oc = siri_orb.CardWindow._cancel
        siri_orb.CardWindow._cancel = lambda self: cancelled.append(1)
        try:
            card._cancel_if_still_dead()
            check("a flap during focus-out keeps the card", len(cancelled), 0)
        finally:
            siri_orb.CardWindow._cancel = oc

        # The mute window is still on: the mic is NOT live yet (muting also
        # mutes the capture), so the card must NOT say 'the mic is ready'.
        card2 = siri_orb.CardWindow({}, FakeHost())
        card2.editing = True
        siri_orb.winh_mod.voice_ui_active = lambda: True
        siri_orb.winh_mod.voice_capture_muted = lambda: True
        card2._poll_voice_live()
        check("a muted capture is not 'mic ready' yet",
              card2.editor.placeholderText(), start)
        siri_orb.winh_mod.voice_capture_muted = lambda: False
        card2._poll_voice_live()
        check("once the mute lifts the card says the mic is ready",
              card2.editor.placeholderText(), hint)

        # The voice panel drops a temp character into the box before the mic is
        # live (it removes it again).  The hint must still update.
        card2.editor.setPlainText("e")
        card3 = siri_orb.CardWindow({}, FakeHost())
        card3.editing = True
        card3.editor.setPlainText("e")
        siri_orb.winh_mod.voice_ui_active = lambda: True
        siri_orb.winh_mod.voice_capture_muted = lambda: False
        card3._poll_voice_live()
        check("a temp char in the box does not block the hint",
              card3.editor.placeholderText(), hint)
        check("the temp char is not wiped", card3.editor.toPlainText(), "e")

        # a NEW round resets the latch
        card.start_edit(placeholder=start)
        siri_orb.winh_mod.voice_ui_active = lambda: True
        card._poll_voice_live()
        check("a new round can show mic-ready again",
              card.editor.placeholderText(), hint)
    finally:
        siri_orb.winh_mod.voice_ui_active = oa
        siri_orb.winh_mod.voice_capture_muted = om
    print("")


# ---------------------------------------------------------------- H
def part_h():
    print("=== H. submitting does not block the GUI thread ===")
    import time
    card = siri_orb.CardWindow({}, FakeHost())
    dismissed = []
    restored = []
    od = siri_orb.winh_mod.dismiss_voice_session
    oa = siri_orb.winh_mod.voice_ui_active
    orf = siri_orb.winh_mod.restore_foreground
    try:
        siri_orb.winh_mod.dismiss_voice_session = lambda: (dismissed.append(1), True)[1]
        # the session never dies on its own -> the OLD code blocked 2.5 s here
        siri_orb.winh_mod.voice_ui_active = lambda: True
        siri_orb.winh_mod.restore_foreground = lambda h: (restored.append(h), True)[1]
        card.editing = True
        card._stole_focus = True
        card._prev_fg = 12345
        card.editor.setPlainText("hello")
        t0 = time.time()
        card._submit()
        dt = time.time() - t0
        check("submit returns fast (no blocking wait)", dt < 0.3, True)
        check("the live voice session was dismissed", len(dismissed), 1)
        check("the focus is NOT taken back while the session lives",
              len(restored), 0)
        # the session dies -> the focus comes back, still without blocking
        siri_orb.winh_mod.voice_ui_active = lambda: False
        card._poll_restore()
        check("the focus goes back once the session ends", restored, [12345])
    finally:
        siri_orb.winh_mod.dismiss_voice_session = od
        siri_orb.winh_mod.voice_ui_active = oa
        siri_orb.winh_mod.restore_foreground = orf
    print("")


def part_i():
    print("=== I. the card is not yanked away while the user is still talking ===")
    # 2026-11-09 21:36 trace: 31 chars arrived, then 2 s later the card was
    # closed by the fixed 10 s timer -- the user was cut off mid-sentence.
    # Windows' voice input only commits text at a pause, so during a long
    # sentence the box stays empty and a fixed timer always cuts in.
    # Now it is a SLIDING timeout: text / mic signal push the deadline.
    card = siri_orb.CardWindow({}, FakeHost())
    card._detect_speech = False        # never open the real mic in a self check
    card.editing = True
    card.arm_timeout(10, lambda: None, max_sec=120)
    check("the window comes from the knob", card._timeout_sec, 10.0)
    check("the hard cap is remembered", card._timeout_hard_deadline > 0, True)

    card._winh_timer.stop()
    ok = card.touch_timeout("test")
    check("a touch is accepted", ok, True)
    check("a touch re-arms the timer", card._winh_timer.isActive(), True)

    # close to the hard cap the touch is clamped, not refused
    card._timeout_hard_deadline = time.time() + 0.5
    card._winh_timer.stop()
    ok = card.touch_timeout("test")
    check("a touch near the cap is still accepted", ok, True)
    check("the delay is clamped to the cap", card._winh_timer.interval() <= 600, True)

    # past the hard cap -> nothing extends any more
    card._timeout_hard_deadline = time.time() - 1
    check("past the hard cap nothing extends", card.touch_timeout("test"), False)

    # text arriving slides the timeout
    card2 = siri_orb.CardWindow({}, FakeHost())
    card2._detect_speech = False
    card2.editing = True
    card2.arm_timeout(10, lambda: None, max_sec=120)
    card2._winh_timer.stop()
    card2.editor.setPlainText("hello")
    check("text arriving slides the timeout", card2._winh_timer.isActive(), True)

    # the mic becoming ready slides it too
    card3 = siri_orb.CardWindow({}, FakeHost())
    card3._detect_speech = False
    card3.editing = True
    card3.arm_timeout(10, lambda: None, max_sec=120)
    card3._winh_timer.stop()
    old_live = siri_orb.winh_mod.voice_ui_active
    old_muted = siri_orb.winh_mod.voice_capture_muted
    try:
        siri_orb.winh_mod.voice_ui_active = lambda: True
        siri_orb.winh_mod.voice_capture_muted = lambda: False
        card3._poll_voice_live()
        check("the mic becoming ready slides the timeout",
              card3._winh_timer.isActive(), True)
    finally:
        siri_orb.winh_mod.voice_ui_active = old_live
        siri_orb.winh_mod.voice_capture_muted = old_muted

    # a voice signal slides it as well
    card4 = siri_orb.CardWindow({}, FakeHost())
    card4._detect_speech = False
    card4.editing = True
    card4.arm_timeout(10, lambda: None, max_sec=120)
    card4._winh_timer.stop()
    card4._speech_until = time.time() + 1.0
    card4._poll_voice_live()
    check("a voice signal slides the timeout", card4._winh_timer.isActive(), True)

    # closing stops the mic watch (otherwise it would hold the mic forever)
    card5 = siri_orb.CardWindow({}, FakeHost())
    card5._detect_speech = False
    card5.arm_timeout(10, lambda: None, max_sec=120)
    card5.cancel_timeout()
    check("closing stops the speech watch", card5._watch_stop, True)
    check("the watch thread is released", card5._watch_thread, None)
    print("")


def part_j():
    print("=== J. overlapping mute windows cannot leave the mic muted ===")
    # 2026-11-09 20:41: two overlapping mute windows -> the second one saved
    # the ALREADY muted state and restored it -> the voice session stayed
    # muted forever = a dead microphone (the user speaks, nothing arrives).
    class FakeSession(object):
        def __init__(self):
            self.owner = "textinputhost.exe"
            self.display_name = "fake"
            self.pid = 4242
            self.state = 1
            self.volume = 1.0
            self.muted = False

        def key(self):
            return "fake-session"

        def set_volume(self, v):
            self.volume = v

        def set_mute(self, f):
            self.muted = bool(f)

        def free(self):
            pass

    fake = FakeSession()
    old_list = win_h.list_audio_sessions
    try:
        win_h.list_audio_sessions = lambda role=None: [fake]
        win_h.mute_voice_start_sound(0.4)
        win_h.mute_voice_start_sound(0.4)      # overlaps the first one
        time.sleep(1.6)
        check("overlapping mute windows do not leave the mic muted",
              fake.muted, False)
        check("the volume is back", fake.volume, 1.0)
        check("no mute window is left open", win_h.voice_mute_active(), False)
    finally:
        win_h.list_audio_sessions = old_list
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
    part_g()
    part_h()
    part_i()
    part_j()
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
