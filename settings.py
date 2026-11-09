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
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PyQt5 import QtCore, QtGui, QtWidgets

import cfgstore
import i18n

try:
    import keys as keylib
except Exception:      # pragma: no cover
    keylib = None

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

QSS = """
QWidget { background:#141419; color:#E8E9F0; font-family:'Microsoft YaHei UI'; font-size:13px; }
QLabel#title { font-size:19px; font-weight:600; color:#FFFFFF; }
QLabel#hint { color:#7C7F8E; font-size:12px; }
QLabel#status { color:#9EA0AE; font-size:12px; }
QLabel#alert { color:#F3B0B0; background:#2A1618; border:1px solid #5E2C30;
                border-radius:6px; padding:8px 10px; font-size:12px; }
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
QCheckBox::indicator:checked { background:#4C5BD4; border-color:#6B79E0; image: url("__CHECK_ICON__"); }
QListWidget { background:#1B1C23; border:1px solid #2E2F3A; border-radius:6px; outline:0; }
QListWidget::item { padding:5px 8px; border-radius:4px; }
QListWidget::item:selected { background:#2E3A78; }
QScrollArea { border:none; background:transparent; }
QScrollBar:vertical { background:#141419; width:10px; margin:0; }
QScrollBar::handle:vertical { background:#33343F; border-radius:5px; min-height:30px; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; }
"""


def _check_icon_path():
    """画一张 16x16 的白色对勾 PNG，给复选框用。

    QSS 画不出字形 —— 没有这个的话，勾选状态只是一块蓝色方块，
    别人根本分不清是「开」还是「关」。放临时目录，进程内缓存。
    """
    try:
        p = os.path.join(tempfile.gettempdir(), "siri_os_check.png")
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < 86400:
            return p
        img = QtGui.QImage(16, 16, QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.transparent)
        pnt = QtGui.QPainter(img)
        pnt.setRenderHint(QtGui.QPainter.Antialiasing, True)
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 240))
        pen.setWidth(2)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        pnt.setPen(pen)
        path = QtGui.QPainterPath()
        path.moveTo(3.5, 8.2)
        path.lineTo(6.6, 11.6)
        path.lineTo(12.4, 4.6)
        pnt.drawPath(path)
        pnt.end()
        img.save(p, "PNG")
        return p
    except Exception:
        return ""


class OrderEditor(QtWidgets.QWidget):
    """后端优先级：上下移动。

    列表里显示的是**厂商友好名**（MiniMax / 千问 / 智谱 GLM…），
    存进 config.json 的是**厂商代号**（minimax / qwen / zhipu…）。
    → 显示名放 text，原始值放 itemData，两边不能混。
    """

    changed = QtCore.pyqtSignal()

    def __init__(self, items, value, lang="zh", items_i18n=None, parent=None):
        super(OrderEditor, self).__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._lang = lang
        self._i18n = items_i18n or {}
        self._items = list(items or [])
        self.list = QtWidgets.QListWidget()
        self.list.setMaximumHeight(240)
        self.set_values(value)
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

    def _label(self, raw):
        m = self._i18n.get(self._lang) or {}
        return m.get(raw, raw)

    def set_values(self, value):
        """回填一串厂商代号 —— 显示名本地化，存回去的还是代号。"""
        vals = [v for v in (value or []) if v]
        for it in self._items:
            if it not in vals:
                vals.append(it)
        self.list.clear()
        for v in vals:
            item = QtWidgets.QListWidgetItem(self._label(v))
            item.setData(QtCore.Qt.UserRole, v)
            self.list.addItem(item)

    def value(self):
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            out.append(it.data(QtCore.Qt.UserRole) or it.text())
        return out

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
        enabled = lcfg.get("enabled") or {}
        order = [n for n in (lcfg.get("order") or [])]
        for n in cfgstore.provider_order():
            if n not in order:
                order.append(n)
        for name in order:
            try:
                if not enabled.get(name, True):
                    out[name] = (i18n.t(self.lang, "settings.backend.off"), "warn")
                    continue
                b = llm.make_backend(name, lcfg)
                if b is None:
                    continue
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
        icon = _check_icon_path()
        icon_css = ('image: url("%s");' % icon.replace(chr(92), "/")) if icon else ""
        self.setStyleSheet(QSS.replace('image: url("__CHECK_ICON__");', icon_css))
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

        # ⚠️ 配置读坏过 → 顶部挂一条红条。
        #    不提示的话，用户在面板点一次「保存」= 原配置被默认值覆盖，而且不可逆。
        try:
            bad_cfg = cfgstore.LAST_ERROR
        except Exception:
            bad_cfg = ""
        if bad_cfg:
            alert = QtWidgets.QLabel(self.T("settings.cfg_bad") % bad_cfg
                                     + "\n" + self.T("settings.cfg_bad.hint"))
            alert.setObjectName("alert")
            alert.setWordWrap(True)
            root.addWidget(alert)

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
        specs = [s for s in cfgstore.SPEC if s.get("tab") == tab]
        if tab == "模型":
            self._fill_model_tab(lay, specs)
            lay.addWidget(self._make_backend_box())
        else:
            for spec in specs:
                lay.addWidget(self._make_row(spec))
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _fill_model_tab(self, lay, specs):
        """模型页：后端优先级在最上，每家厂商一个分组，系统提示词在最后。"""
        flat = [s for s in specs if not s.get("provider")]
        groups = {}
        for s in specs:
            if s.get("provider"):
                groups.setdefault(s["provider"], []).append(s)
        for s in flat:
            if s["key"] == "llm.order":
                lay.addWidget(self._make_row(s))
        for name in cfgstore.provider_order():
            rows = groups.get(name)
            if rows:
                lay.addWidget(self._make_provider_group(name, rows))
        for s in flat:
            if s["key"] != "llm.order":
                lay.addWidget(self._make_row(s))

    def _make_provider_group(self, name, rows):
        """一家厂商一个分组框 —— 8 家摊平成 32 行根本没法看。"""
        grp = QtWidgets.QGroupBox(cfgstore.provider_label(name, self.lang()))
        gl = QtWidgets.QGridLayout(grp)
        gl.setHorizontalSpacing(12)
        gl.setVerticalSpacing(5)
        gl.setContentsMargins(12, 8, 12, 10)
        gl.setColumnStretch(1, 1)
        r = 0
        for spec in rows:
            lab = QtWidgets.QLabel(self._spec_text(spec, "label", "en_label"))
            lab.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            ctrl = self._make_control(spec)
            self.rows.append((spec, ctrl))
            gl.addWidget(lab, r, 0)
            gl.addWidget(self._wrap_control(spec, ctrl), r, 1)
            r += 1
            hint = self._spec_text(spec, "hint", "en_hint")
            if hint:
                hl = QtWidgets.QLabel(hint)
                hl.setObjectName("hint")
                hl.setWordWrap(True)
                gl.addWidget(hl, r, 0, 1, 2)
                r += 1
        return grp

    def _secret_len(self, spec):
        """密钥库里已存的 key 长度 —— **只用来做占位提示，不显示内容**。"""
        name = spec.get("secret_name") or spec["key"].split(".")[-1]
        try:
            if keylib is None:
                return 0
            return int(keylib.summary().get(name, 0))
        except Exception:
            return 0

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

        ctrl = self._make_control(spec)
        self.rows.append((spec, ctrl))
        lay.addWidget(self._wrap_control(spec, ctrl))

        hint = self._spec_text(spec, "hint", "en_hint")
        if hint:
            hl = QtWidgets.QLabel(hint)
            hl.setObjectName("hint")
            hl.setWordWrap(True)
            lay.addWidget(hl)
        return w

    def _make_control(self, spec):
        """按 SPEC 造控件。抽出来是为了让「分组框」和「普通行」共用同一套。"""
        t = spec["type"]
        cur = cfgstore.get_path(self.cfg, spec["key"])
        if spec["key"] == "autostart":
            # 开机自启显示**真实状态**（启动文件夹里那个快捷方式在不在），
            # 不是配置文件里的历史值 —— 否则别人手动删了快捷方式面板还骗人
            try:
                import install_autostart as ia
                cur = ia.is_installed()
            except Exception:
                pass
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
        elif t == "secret":
            ctrl = QtWidgets.QLineEdit()
            ctrl.setEchoMode(QtWidgets.QLineEdit.Password)
            ctrl.setPlaceholderText(self.T("settings.secret.have") % self._secret_len(spec))
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
            ctrl = OrderEditor(spec.get("items", []), cur, self.lang(),
                               spec.get("items_i18n"))
            ctrl.changed.connect(self._on_change)
        else:
            ctrl = QtWidgets.QLabel("-")
        return ctrl

    def _wrap_control(self, spec, ctrl):
        """模型下拉旁边挂一个「拉取最新」按钮 —— 新模型上线不用等发版。"""
        if not (spec.get("provider") and spec["type"] == "choice"):
            return ctrl
        brow = QtWidgets.QHBoxLayout()
        brow.setContentsMargins(0, 0, 0, 0)
        brow.setSpacing(8)
        brow.addWidget(ctrl, 1)
        btn = QtWidgets.QPushButton(self.T("settings.models.refresh"))
        btn.setToolTip(self.T("settings.models.refresh.tip"))
        btn.clicked.connect(lambda _c=False, sp=spec, c=ctrl: self._refresh_models(sp, c))
        brow.addWidget(btn)
        box = QtWidgets.QWidget()
        box.setLayout(brow)
        return box

    def _set_choice(self, ctrl, spec, value):
        """下拉回填：本地化选项用 findData 定位，其余用 setCurrentText。"""
        if spec.get("choices_i18n") and not spec.get("editable"):
            idx = ctrl.findData(str(value))
            if idx >= 0:
                ctrl.setCurrentIndex(idx)
            return
        # 可编辑的下拉（模型名）：**先按值定位到对应项**，这样展开时高亮的是当前模型，
        # 而不是第 1 项；清单里没有（用户手输过的）再回填纯文本。
        if spec.get("editable"):
            idx = ctrl.findText(str(value if value is not None else ""),
                                QtCore.Qt.MatchFixedString)
            if idx >= 0:
                ctrl.setCurrentIndex(idx)
                return
        ctrl.setCurrentText(str(value if value is not None else ""))

    def _make_backend_box(self):
        grp = QtWidgets.QGroupBox(self.T("settings.backends.title"))
        gl = QtWidgets.QVBoxLayout(grp)
        gl.setSpacing(6)
        for name in cfgstore.provider_order():
            disp = cfgstore.provider_label(name, self.lang())
            lab = QtWidgets.QLabel(self.T("settings.backends.checking") % disp)
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
            cfg, _secrets = self._collect()
            cfgstore.save(cfg)
            self.cfg = cfg
        except Exception:
            pass
        QtCore.QTimer.singleShot(0, self._rebuild_for_lang)

    def _refresh_models(self, spec, ctrl):
        """从服务商自己的 /models 端点拉一份当前可用清单，填进下拉框。

        拉失败**不动现有列表** —— 宁可少几个，也不要把用户能用的清掉。
        """
        prov = spec.get("provider")
        try:
            import llm as llm_mod
            models, err = llm_mod.list_models(prov)
        except Exception as e:
            models, err = [], str(e)
        if not models:
            self._set_status(self.T("settings.models.refresh_failed") % (err or "?"))
            return
        cur = ctrl.currentText().strip()
        # **并集**，不是整表替换：服务端列表可能不含还能调通的旧别名
        # （DeepSeek 的 chat / reasoner / coder 就不在 /models 里，但照样能用）。
        # 整表替换会把用户能用的模型抹掉 —— 宁可多列几个，不可少列。
        have = [ctrl.itemText(i) for i in range(ctrl.count())]
        merged = list(have)
        added = 0
        for m in models:
            if m not in merged:
                merged.append(m)
                added += 1
        if cur and cur not in merged:
            merged.append(cur)              # 保留用户手输的模型名
        ctrl.blockSignals(True)
        try:
            ctrl.clear()
            for m in merged:
                ctrl.addItem(m)
            idx = ctrl.findText(cur, QtCore.Qt.MatchFixedString) if cur else -1
            if idx >= 0:
                ctrl.setCurrentIndex(idx)      # 高亮仍然是当前模型
            else:
                ctrl.setCurrentText(cur)
        finally:
            ctrl.blockSignals(False)
        self._set_status(self.T("settings.models.refreshed") % (len(models), len(merged), added))

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
        """收集所有控件的值，返回 (cfg, secrets)。

        - cfg 写进 config.json
        - secrets 是这次**新填的**密钥（留空 = 不改），单独写进密钥库。
          ⚠️ 密钥永远不进 config.json —— config.json 是要进版本库的东西。
        """
        cfg = json.loads(json.dumps(self.cfg))
        secrets = {}
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
            elif t == "secret":
                raw = ctrl.text().strip()
                if raw:
                    secrets[spec.get("secret_name") or spec["key"].split(".")[-1]] = raw
                continue
            else:
                continue
            cfgstore.set_path(cfg, spec["key"], val)
        return cfg, secrets

    def _set_status(self, msg):
        self._status_msg = msg
        try:
            self.lbl_status.setText(msg)
        except Exception:
            pass

    def _do_save(self):
        try:
            cfg, secrets = self._collect()
            cfgstore.save(cfg)
            self.cfg = cfg
            # 密钥单独写进密钥库（永远不进 config.json）
            saved = []
            for name, val in secrets.items():
                if keylib is None:
                    raise RuntimeError("keys module unavailable")
                keylib.set_key(name, val)
                saved.append(name)
            if saved:
                # 填了 key 就把该厂商**自动启用** —— 否则用户填完 key 还要再勾一次
                # 「启用」，很容易以为没生效。
                for name in saved:
                    cfgstore.set_path(cfg, "llm.enabled.%s" % name, True)
                    for sp2, c2 in self.rows:
                        if sp2.get("key") == "llm.enabled.%s" % name and sp2.get("type") == "bool":
                            c2.setChecked(True)
                for spec, ctrl in self.rows:
                    if spec["type"] != "secret":
                        continue
                    nm = spec.get("secret_name") or spec["key"].split(".")[-1]
                    if nm in saved:
                        ctrl.clear()
                        ctrl.setPlaceholderText(self.T("settings.secret.have") % self._secret_len(spec))
                cfgstore.save(cfg)
                self.cfg = cfg
                self._set_status(self.T("settings.secret.saved") % ", ".join(saved))
                self._backend_check()          # 存完立刻验活
            else:
                self._set_status(self.T("settings.saved"))
            self._apply_autostart(cfg)
            return True
        except Exception as e:
            self._set_status(self.T("settings.save_failed") % e)
            return False

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
                ctrl.set_values(d or spec.get("items", []))
            elif t == "secret":
                ctrl.clear()                   # 只清输入框，不动密钥库里已存的 key
                ctrl.setPlaceholderText(self.T("settings.secret.have") % self._secret_len(spec))
        self._do_save()
        self._set_status(self.T("settings.reset_done"))
        if self.lang() != before:
            QtCore.QTimer.singleShot(0, self._rebuild_for_lang)

    def _apply_autostart(self, cfg):
        """开机自启开关：按配置装/卸「启动」文件夹里的快捷方式。"""
        want = bool(cfg.get("autostart", True))
        try:
            import install_autostart as ia
            have = ia.is_installed()
        except Exception:
            return
        if want == have:
            return
        try:
            if pathmod is not None and pathmod.is_frozen():
                # 打包后没有独立的 install_autostart.py 可执行 —— 直接在进程里调
                import install_autostart as ia
                if want:
                    ia.install()
                else:
                    ia.remove()
            else:
                here = os.path.dirname(os.path.abspath(__file__))
                cmd = [sys.executable, os.path.join(here, "install_autostart.py"),
                       "install" if want else "remove"]
                subprocess.run(cmd, cwd=here, capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._set_status(self.T("settings.autostart.on" if want else "settings.autostart.off"))
        except Exception as e:
            self._set_status(self.T("settings.autostart.failed") % e)

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
        # 打包成 exe 之后没有控制台，set_key.py 的 getpass 读不到东西 ——
        # 但**面板里的密钥输入框一样能用**（每家厂商一个，保存即写入密钥库）。
        if pathmod is not None and pathmod.is_frozen():
            try:
                tab = self._tab_widgets.get("模型")
                if tab is not None:
                    self.tabs.setCurrentWidget(tab)
                self._set_status(self.T("settings.keys.inpanel"))
                return
            except Exception as e:
                self._set_status(self.T("settings.keys.failed") % e)
                return
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
            disp = cfgstore.provider_label(name, self.lang())
            lab.setText("%s: %s" % (disp, txt) if self.lang() == "en"
                        else "%s：%s" % (disp, txt))
            lab.setStyleSheet("color: %s; font-size:12px;" % colors.get(kind, "#9EA0AE"))

    def _refresh_resident(self):
        pid = cfgstore.resident_pid()
        if pid:
            self.lbl_resident.setText(self.T("settings.resident.running") % pid)
        else:
            self.lbl_resident.setText(self.T("settings.resident.stopped"))

    def closeEvent(self, ev):
        self._save_timer.stop()
        ok = True
        try:
            ok = self._do_save()
        except Exception:
            ok = False
        if not ok:
            # ⚠️ 保存失败不能闷声把窗口关掉 —— 用户会以为设置存上了。
            try:
                QtWidgets.QMessageBox.warning(
                    self, self.T("settings.title"),
                    self.T("settings.save_failed_popup") % self._status_msg)
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
