# -*- coding: utf-8 -*-
"""Siri-OS 设置面板（独立窗口）。

为什么做成独立进程，而不是常驻助手里的弹窗：
  - 助手没在跑的时候也能打开改配置（改完等助手起来就生效）；
  - 面板崩了不影响助手；
  - 能做成 .cmd 双击打开，不挑终端、不用记命令。

生效机制：面板原子写 config.json -> 常驻助手每 0.5 秒按 mtime 热重载
-> **存完 ≤1 秒自动生效，不用重启**。模型/后端顺序改了也会立刻重建后端列表。

界面是**照着 cfgstore.SPEC 自动生成**的：以后加一个可调项只要往 SPEC 里加一行。

🈳 中英双语（2026-11-07）：整个面板跟着 ui_lang 走 —— 页签名、标签、提示、
   单位、下拉选项、按钮文案全部本地化。改「界面语言」这一项会**重建整块界面**
   （不是只换几个字），所以别的地方也必须走 i18n.t()，别在代码里写死中文。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PyQt5 import QtCore, QtGui, QtWidgets

import cfgstore
import i18n

QSS = """
QWidget { background:#141419; color:#E8E9F0; font-family:'Microsoft YaHei UI'; font-size:13px; }
QLabel#title { font-size:19px; font-weight:600; color:#FFFFFF; }
QLabel#hint { color:#7C7F8E; font-size:12px; }
QLabel#status { color:#9EA0AE; font-size:12px; }
QTabWidget::pane { border:1px solid #26272F; border-radius:8px; top:-1px; }
QTabBar::tab { background:#1B1C23; color:#9EA0AE; padding:7px 18px; border:1px solid #26272F;
               border-bottom:none; border-top-left-radius:8px; border-top-right-radius:8px; margin-right:2px; }
QTabBar::tab:selected { background:#141419; color:#FFFFFF; }
QGroupBox { border:1px solid #26272F; border-radius:8px; margin-top:14px; padding:10px 12px 8px 12px; }
QGroupBox::title { subcontrol-origin: margin; left:12px; padding:0 6px; color:#9EA0AE; }
QSlider::groove:horizontal { height:4px; background:#2A2B35; border-radius:2px; }
QSlider::sub-page:horizontal { background:#4C5BD4; border-radius:2px; }
QSlider::handle:horizontal { background:#8C9BFF; width:14px; height:14px; margin:-6px 0; border-radius:7px; }
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit {
  background:#1B1C23; border:1px solid #2E2F3A; border-radius:6px; padding:4px 8px; color:#E8E9F0;
  selection-background-color:#4C5BD4; }
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus, QPlainTextEdit:focus {
  border:1px solid #7C8CFF; }
QComboBox QAbstractItemView { background:#1B1C23; color:#E8E9F0; selection-background-color:#4C5BD4; }
QPushButton { background:#23242D; border:1px solid #33343F; border-radius:6px; padding:6px 14px; color:#E8E9F0; }
QPushButton:hover { background:#2C2D38; border-color:#45465A; }
QPushButton:pressed { background:#1E1F27; }
QCheckBox { color:#E8E9F0; spacing:8px; }
QCheckBox::indicator { width:16px; height:16px; border-radius:4px;
                       border:1px solid #3A3B47; background:#1B1C23; }
QCheckBox::indicator:hover { border-color:#7C8CFF; }
QCheckBox::indicator:checked { background:#4C5BD4; border-color:#6B79E0; }
QListWidget { background:#1B1C23; border:1px solid #2E2F3A; border-radius:6px; outline:0; }
QListWidget::item { padding:5px 8px; border-radius:4px; }
QListWidget::item:selected { background:#2E3A78; }
QScrollArea { border:none; background:transparent; }
QScrollBar:vertical { background:#141419; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#33343F; border-radius:5px; min-height:30px; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; }
"""


class OrderEditor(QtWidgets.QWidget):
    """后端优先级：上下移动。"""

    changed = QtCore.pyqtSignal()

    def __init__(self, items, value, lang="zh", parent=None):
        super(OrderEditor, self).__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.list = QtWidgets.QListWidget()
        self.list.setMaximumHeight(122)
        vals = [v for v in (value or []) if v]
        for it in items:
            if it not in vals:
                vals.append(it)
        self.list.addItems(vals)
        lay.addWidget(self.list)
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        up = QtWidgets.QPushButton(i18n.t(lang, "settings.order.up"))
        down = QtWidgets.QPushButton(i18n.t(lang, "settings.order.down"))
        up.clicked.connect(self._up)
        down.clicked.connect(self._down)
        row.addWidget(up)
        row.addWidget(down)
        row.addStretch(1)
        lay.addLayout(row)

    def _up(self):
        i = self.list.currentRow()
        if i > 0:
            it = self.list.takeItem(i)
            self.list.insertItem(i - 1, it)
            self.list.setCurrentRow(i - 1)
            self.changed.emit()

    def _down(self):
        i = self.list.currentRow()
        if 0 <= i < self.list.count() - 1:
            it = self.list.takeItem(i)
            self.list.insertItem(i + 1, it)
            self.list.setCurrentRow(i + 1)
            self.changed.emit()

    def value(self):
        return [self.list.item(i).text() for i in range(self.list.count())]


class NumberRow(QtWidgets.QWidget):
    """滑块 + 数值框（联动）。滑块位置是整数，值由 min + pos*step 算出来。"""

    changed = QtCore.pyqtSignal()

    def __init__(self, spec, value, parent=None):
        super(NumberRow, self).__init__(parent)
        self._loading = False
        self.spec = spec
        self.is_int = spec["type"] == "int"
        self.lo = float(spec.get("min", 0))
        self.hi = float(spec.get("max", 1))
        self.step = float(spec.get("step", 1)) or 1.0
        n = max(1, int(round((self.hi - self.lo) / self.step)))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, n)
        lay.addWidget(self.slider, 1)
        if self.is_int:
            self.spin = QtWidgets.QSpinBox()
            self.spin.setRange(int(self.lo), int(self.hi))
            self.spin.setSingleStep(max(1, int(self.step)))
        else:
            self.spin = QtWidgets.QDoubleSpinBox()
            self.spin.setDecimals(int(spec.get("decimals", 2)))
            self.spin.setRange(self.lo, self.hi)
            self.spin.setSingleStep(self.step)
        self.spin.setFixedWidth(112)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.set_value(value)

    def set_value(self, v):
        self._loading = True
        try:
            try:
                fv = float(v)
            except Exception:
                fv = self.lo
            fv = max(self.lo, min(self.hi, fv))
            if self.is_int:
                self.spin.setValue(int(round(fv)))
            else:
                self.spin.setValue(fv)
            self.slider.setValue(int(round((float(self.spin.value()) - self.lo) / self.step)))
        finally:
            self._loading = False

    def _from_slider(self, pos):
        if self._loading:
            return
        self._loading = True
        try:
            fv = self.lo + pos * self.step
            if self.is_int:
                self.spin.setValue(int(round(fv)))
            else:
                self.spin.setValue(fv)
        finally:
            self._loading = False
        self.changed.emit()

    def _from_spin(self, _v):
        if self._loading:
            return
        self._loading = True
        try:
            self.slider.setValue(int(round((float(self.spin.value()) - self.lo) / self.step)))
        finally:
            self._loading = False
        self.changed.emit()

    def value(self):
        return int(self.spin.value()) if self.is_int else float(self.spin.value())


class BackendCheck(QtCore.QThread):
    """后端可用性自检（后台线程，不能卡住界面）。"""

    done = QtCore.pyqtSignal(dict)

    def __init__(self, cfg, lang="zh", parent=None):
        super(BackendCheck, self).__init__(parent)
        self.cfg = cfg
        self.lang = lang

    def run(self):
        out = {}
        try:
            import llm
        except Exception as e:
            self.done.emit({"_": (i18n.t(self.lang, "settings.import_error") % e, "bad")})
            return
        lcfg = self.cfg.get("llm") or {}
        makers = [
            ("minimax", lambda: llm.MinimaxBackend(
                model=lcfg.get("minimax_model"),
                api_key=lcfg.get("minimax_api_key"),
                base_url=lcfg.get("minimax_base_url"))),
            ("deepseek", lambda: llm.DeepSeekBackend(
                model=lcfg.get("deepseek_model"),
                api_key=lcfg.get("deepseek_api_key"),
                base_url=lcfg.get("deepseek_base_url"))),
            ("zhipu", lambda: llm.ZhipuBackend(
                model=lcfg.get("zhipu_model"),
                api_key=lcfg.get("zhipu_api_key"),
                base_url=lcfg.get("zhipu_base_url"))),
        ]
        for name, mk in makers:
            try:
                b = mk()
                try:
                    b.refresh()
                except Exception:
                    pass
                if not b.available():
                    out[name] = (i18n.t(self.lang, "settings.backend.no_key"), "warn")
                    continue
                txt = b.chat([{"role": "user", "content": "你好"}],
                             max_tokens=8, temperature=0)
                out[name] = (i18n.t(self.lang, "settings.backend.ok")
                             + (txt.strip()[:16] or "200 OK"), "ok")
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if "402" in msg or "insufficient" in low:
                    out[name] = (i18n.t(self.lang, "settings.backend.balance"), "warn")
                elif "401" in msg or "403" in msg:
                    out[name] = (i18n.t(self.lang, "settings.backend.auth"), "bad")
                else:
                    out[name] = (i18n.t(self.lang, "settings.backend.failed") + msg[:70], "bad")
        self.done.emit(out)


def dark_titlebar(win):
    """Windows 深色标题栏。

    不走「无边框自己画标题栏」那条路（那要自己处理拖拽/最小化/关闭，容易出 bug），
    直接用 DWM 的暗色标题栏开关 —— 一行 API，效果一样。
    """
    try:
        import ctypes
        hwnd = int(win.winId())
        val = ctypes.c_int(1)
        for attr in (20, 19):        # 20 = Win11 22000+，19 = Win10 1809+
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
            if hr == 0:
                break
    except Exception:
        pass


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self):
        super(SettingsDialog, self).__init__()
        self.setMinimumSize(840, 680)
        self.cfg = cfgstore.load()
        self.rows = []                 # [(spec, 控件)]
        self.backend_labels = {}
        self._tab_widgets = {}
        self._checker = None
        self._status_msg = ""
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._do_save)

        self._central = QtWidgets.QWidget(self)
        self._central.setLayout(QtWidgets.QVBoxLayout(self._central))
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._central)

        self._build()
        self.setStyleSheet(QSS)
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.timeout.connect(self._refresh_resident)
        self._status_timer.start(2000)
        self._refresh_resident()
        self._backend_check()

    # ------------------------------------------------------------ 语言
    def lang(self):
        return i18n.ui_lang(self.cfg)

    def T(self, key):
        return i18n.t(self.lang(), key)

    def _spec_text(self, spec, zh_key, en_key):
        """取 SPEC 里的文案：英文模式下用 en_*，没有就退回中文。"""
        if self.lang() == "en":
            v = spec.get(en_key)
            if v:
                return v
        return spec.get(zh_key) or ""

    # ------------------------------------------------------------ 界面
    def _build(self):
        """按当前语言重建整块界面（改「界面语言」时会重来一遍）。

        ⚠ 重建必须把**上一版整棵控件树**一起拆掉（2026-11-07 实测踩过）：
        旧写法只对**顶层布局项**做 `setParent(None)`，可**嵌套布局里的控件**
        （标题行、底部按钮行）根本不在顶层项里 —— 它们会留下来继续画在屏幕上，
        表现就是「中文按钮压在新建的英文按钮上」（实测按钮数 7 → 11）。

        现在整块换成一个容器：旧容器 hide → removeWidget → setParent(None) →
        deleteLater，整棵子树跟着走，不会留任何残影。
        """
        old = getattr(self, "_content", None)
        if old is not None:
            old.hide()
            try:
                self._central.layout().removeWidget(old)
            except Exception:
                pass
            old.setParent(None)
            old.deleteLater()

        self.rows = []
        self.backend_labels = {}
        self._tab_widgets = {}

        lang = self.lang()
        self.setWindowTitle(self.T("settings.title"))

        self._content = QtWidgets.QWidget(self._central)
        self._content.setLayout(QtWidgets.QVBoxLayout(self._content))
        root = self._content.layout()
        self._central.layout().addWidget(self._content)

        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(self.T("settings.title"))
        title.setObjectName("title")
        head.addWidget(title)
        head.addStretch(1)
        self.lbl_resident = QtWidgets.QLabel(self.T("settings.resident.checking"))
        self.lbl_resident.setObjectName("status")
        head.addWidget(self.lbl_resident)
        root.addLayout(head)

        sub = QtWidgets.QLabel(self.T("settings.subtitle"))
        sub.setObjectName("hint")
        root.addWidget(sub)

        self.tabs = QtWidgets.QTabWidget()
        for tab in cfgstore.TABS:
            w = self._make_tab(tab)
            self._tab_widgets[tab] = w
            name = cfgstore.TAB_EN.get(tab, tab) if lang == "en" else tab
            self.tabs.addTab(w, name)
        root.addWidget(self.tabs, 1)

        self.lbl_status = QtWidgets.QLabel(self._status_msg)
        self.lbl_status.setObjectName("status")
        root.addWidget(self.lbl_status)

        row = QtWidgets.QHBoxLayout()
        b_demo = QtWidgets.QPushButton(self.T("settings.btn.demo"))
        b_demo.setToolTip(self.T("settings.btn.demo.tip"))
        b_demo.clicked.connect(self._demo)
        b_key = QtWidgets.QPushButton(self.T("settings.btn.keys"))
        b_key.setToolTip(self.T("settings.btn.keys.tip"))
        b_key.clicked.connect(self._open_keys)
        b_reset = QtWidgets.QPushButton(self.T("settings.btn.reset"))
        b_reset.setToolTip(self.T("settings.btn.reset.tip"))
        b_reset.clicked.connect(self._reset_defaults)
        b_close = QtWidgets.QPushButton(self.T("settings.btn.close"))
        b_close.clicked.connect(self.close)
        for b in (b_demo, b_key, b_reset):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
        root.addLayout(row)

    def _make_tab(self, tab):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(inner)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(14)
        for spec in cfgstore.SPEC:
            if spec.get("tab") != tab:
                continue
            lay.addWidget(self._make_row(spec))
        if tab == "模型":
            lay.addWidget(self._make_backend_box())
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _make_row(self, spec):
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        lab = QtWidgets.QLabel(self._spec_text(spec, "label", "en_label"))
        top.addWidget(lab)
        top.addStretch(1)
        unit = self._spec_text(spec, "unit", "en_unit")
        if unit:
            u = QtWidgets.QLabel(unit)
            u.setObjectName("hint")
            top.addWidget(u)
        lay.addLayout(top)

        t = spec["type"]
        cur = cfgstore.get_path(self.cfg, spec["key"])
        if t in ("float", "int"):
            ctrl = NumberRow(spec, cur)
            ctrl.changed.connect(self._on_change)
        elif t == "bool":
            ctrl = QtWidgets.QCheckBox()
            ctrl.setChecked(bool(cur))
            ctrl.stateChanged.connect(self._on_change)
        elif t == "text":
            ctrl = QtWidgets.QLineEdit(str(cur if cur is not None else ""))
            ctrl.editingFinished.connect(self._on_change)
        elif t == "textlong":
            ctrl = QtWidgets.QPlainTextEdit(str(cur if cur is not None else ""))
            ctrl.setMinimumHeight(92)
            ctrl.textChanged.connect(self._on_change)
        elif t == "choice":
            ctrl = QtWidgets.QComboBox()
            cmap = ((spec.get("choices_i18n") or {}).get(self.lang())) or {}
            for c in spec.get("choices", []):
                if cmap:
                    ctrl.addItem(cmap.get(c, c), c)     # 显示本地化，存原始值
                else:
                    ctrl.addItem(c)
            if spec.get("editable"):
                ctrl.setEditable(True)
            self._set_choice(ctrl, spec, cur)
            if spec.get("key") == "ui_lang":
                ctrl.currentTextChanged.connect(self._on_ui_lang_changed)
            else:
                ctrl.currentTextChanged.connect(self._on_change)
        elif t == "order":
            ctrl = OrderEditor(spec.get("items", []), cur, self.lang())
            ctrl.changed.connect(self._on_change)
        else:
            ctrl = QtWidgets.QLabel("-")
        lay.addWidget(ctrl)

        hint = self._spec_text(spec, "hint", "en_hint")
        if hint:
            hl = QtWidgets.QLabel(hint)
            hl.setObjectName("hint")
            hl.setWordWrap(True)
            lay.addWidget(hl)

        self.rows.append((spec, ctrl))
        return w

    def _set_choice(self, ctrl, spec, value):
        """下拉回填：本地化选项用 findData 定位，其余用 setCurrentText。"""
        if spec.get("choices_i18n") and not spec.get("editable"):
            idx = ctrl.findData(str(value))
            if idx >= 0:
                ctrl.setCurrentIndex(idx)
            return
        ctrl.setCurrentText(str(value if value is not None else ""))

    def _make_backend_box(self):
        grp = QtWidgets.QGroupBox(self.T("settings.backends.title"))
        gl = QtWidgets.QVBoxLayout(grp)
        gl.setSpacing(6)
        for name in ("minimax", "deepseek", "zhipu"):
            lab = QtWidgets.QLabel(self.T("settings.backends.checking") % name)
            lab.setObjectName("status")
            gl.addWidget(lab)
            self.backend_labels[name] = lab
        btn = QtWidgets.QPushButton(self.T("settings.backends.recheck"))
        btn.clicked.connect(self._backend_check)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(btn)
        row.addStretch(1)
        gl.addLayout(row)
        return grp

    # ------------------------------------------------------------ 保存
    def _on_change(self, *_a):
        self._save_timer.start()

    def _on_ui_lang_changed(self, _val):
        """界面语言变了：先存盘，再整块重建界面。"""
        try:
            cfg = self._collect()
            cfgstore.save(cfg)
            self.cfg = cfg
        except Exception:
            pass
        QtCore.QTimer.singleShot(0, self._rebuild_for_lang)

    def _rebuild_for_lang(self):
        keep = None
        try:
            keep = cfgstore.TABS[self.tabs.currentIndex()]
        except Exception:
            pass
        self.cfg = cfgstore.load()
        self._build()
        if keep in self._tab_widgets:
            self.tabs.setCurrentWidget(self._tab_widgets[keep])
        self._refresh_resident()

    def _collect(self):
        cfg = json.loads(json.dumps(self.cfg))
        for spec, ctrl in self.rows:
            t = spec["type"]
            if t in ("float", "int"):
                val = ctrl.value()
            elif t == "bool":
                val = bool(ctrl.isChecked())
            elif t == "text":
                val = ctrl.text()
            elif t == "textlong":
                val = ctrl.toPlainText()
            elif t == "choice":
                if spec.get("choices_i18n") and not spec.get("editable"):
                    val = ctrl.currentData()
                else:
                    val = ctrl.currentText().strip()
            elif t == "order":
                val = ctrl.value()
            else:
                continue
            cfgstore.set_path(cfg, spec["key"], val)
        return cfg

    def _set_status(self, msg):
        self._status_msg = msg
        try:
            self.lbl_status.setText(msg)
        except Exception:
            pass

    def _do_save(self):
        try:
            cfg = self._collect()
            cfgstore.save(cfg)
            self.cfg = cfg
            self._set_status(self.T("settings.saved"))
        except Exception as e:
            self._set_status(self.T("settings.save_failed") % e)

    def _reset_defaults(self):
        before = self.lang()
        for spec, ctrl in self.rows:
            d = cfgstore.get_path(cfgstore.DEFAULTS, spec["key"])
            t = spec["type"]
            if t in ("float", "int"):
                ctrl.set_value(d)
            elif t == "bool":
                ctrl.setChecked(bool(d))
            elif t == "text":
                ctrl.setText(str(d if d is not None else ""))
            elif t == "textlong":
                ctrl.setPlainText(str(d if d is not None else ""))
            elif t == "choice":
                self._set_choice(ctrl, spec, d)
            elif t == "order":
                ctrl.list.clear()
                ctrl.list.addItems(d or spec.get("items", []))
        self._do_save()
        self._set_status(self.T("settings.reset_done"))
        if self.lang() != before:
            QtCore.QTimer.singleShot(0, self._rebuild_for_lang)

    # ------------------------------------------------------------ 功能
    def _demo(self):
        try:
            cfgstore.fire_trigger()
            pid = cfgstore.resident_pid()
            if pid:
                self._set_status(self.T("settings.demo.ok") % pid)
            else:
                self._set_status(self.T("settings.demo.no_resident"))
        except Exception as e:
            self._set_status(self.T("settings.demo.failed") % e)

    def _open_keys(self):
        script = os.path.join(HERE, "set_key.py")
        try:
            # ⚠️ 必须用带控制台的 python.exe：set_key.py 用 getpass 读 key，
            # 而常驻进程里的 sys.executable 是 pythonw.exe（无控制台）→ getpass 会失败。
            py = os.path.join(os.path.dirname(sys.executable), "python.exe")
            if not os.path.exists(py):
                py = sys.executable
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen([py, script], cwd=HERE, creationflags=flags)
            self._set_status(self.T("settings.keys.opened"))
        except Exception as e:
            self._set_status(self.T("settings.keys.failed") % e)

    def _backend_check(self):
        if self._checker is not None and self._checker.isRunning():
            return
        for name, lab in self.backend_labels.items():
            lab.setText(self.T("settings.backends.checking") % name)
        self._checker = BackendCheck(self.cfg, self.lang())
        self._checker.done.connect(self._on_backend_done)
        self._checker.start()

    def _on_backend_done(self, out):
        colors = {"ok": "#7FD1A0", "warn": "#E8C36A", "bad": "#E88A8A"}
        for name, (txt, kind) in out.items():
            lab = self.backend_labels.get(name)
            if lab is None:
                continue
            lab.setText("%s: %s" % (name, txt) if self.lang() == "en"
                        else "%s：%s" % (name, txt))
            lab.setStyleSheet("color: %s; font-size:12px;" % colors.get(kind, "#9EA0AE"))

    def _refresh_resident(self):
        pid = cfgstore.resident_pid()
        if pid:
            self.lbl_resident.setText(self.T("settings.resident.running") % pid)
        else:
            self.lbl_resident.setText(self.T("settings.resident.stopped"))

    def closeEvent(self, ev):
        self._save_timer.stop()
        try:
            self._do_save()
        except Exception:
            pass
        super(SettingsDialog, self).closeEvent(ev)


def main():
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication(sys.argv)
    dlg = SettingsDialog()
    dlg.show()
    dark_titlebar(dlg)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
