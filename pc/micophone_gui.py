"""Micophone for Windows: the window, tray icon and settings around receiver.Engine.

Run from source:   python micophone_gui.py
Build the exe:     build.bat
README screenshot: python micophone_gui.py --demo --theme dark --screenshot ../docs/images/pc-dark.png
                   (--demo shows made-up data, so no real PC name, address or phone ends up in an image)
"""
import ctypes
import getpass
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
import winreg

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer
from PyQt6.QtGui import (QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QRadialGradient)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
                             QLabel, QMenu, QMessageBox, QPlainTextEdit, QProxyStyle, QPushButton, QStyle,
                             QSystemTrayIcon, QVBoxLayout, QWidget)

import receiver as rx
import theme

VERSION = "2.0.0"
REPO_URL = "https://github.com/farzonline/micophone"
VBCABLE_URL = "https://vb-audio.com/Cable/"
SETTINGS = rx.APP_DIR / "settings.json"
LOG_FILE = rx.APP_DIR / "micophone.log"
AUTO = "Automatic (VB-CABLE if installed)"
FW_RULE = "Micophone"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
INSTANCE_KEY = f"Micophone-{getpass.getuser()}"  # a second launch asks this one to show itself
LATENCY = {"low": ("Low", 20), "normal": ("Normal", 40), "stable": ("Stable", 80)}

P = dict(theme.PALETTES["dark"])  # the active palette; widgets read it when they paint

# Segoe Fluent Icons (Windows 11) or Segoe MDL2 Assets (Windows 10) glyphs.
GLYPH = {"minimize": "", "close": "", "refresh": ""}


# ---------------------------------------------------------------- settings & system bits

def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(s):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(s, indent=2))
    except OSError:
        pass


def firewall_allowed():
    for name in (FW_RULE, "PhoneMic"):  # "PhoneMic" = a rule made by hand for an early version
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                           capture_output=True, creationflags=rx.NO_WINDOW)
        if r.returncode == 0:
            return True
    return False


def allow_firewall():
    """Inbound UDP rule on all network profiles (one UAC prompt). Re-creates it if present."""
    fw = "netsh advfirewall firewall"
    cmd = (f'/c {fw} delete rule name="{FW_RULE}" >nul & {fw} add rule name="{FW_RULE}" '
           f"dir=in action=allow protocol=UDP localport={rx.PORT} profile=any")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd, None, 0)


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw}" "{os.path.abspath(__file__)}" --minimized'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "Micophone")
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "Micophone", 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, "Micophone")
            except FileNotFoundError:
                pass


def dwm_frame(widget):
    """Windows 11: rounded corners and a hairline border in the theme's colour. No-op elsewhere."""
    try:
        hwnd = int(widget.winId())
        corner = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
        c = QColor(P["line"])
        border = ctypes.c_uint(c.red() | c.green() << 8 | c.blue() << 16)  # COLORREF is 0x00BBGGRR
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
    except (AttributeError, OSError):
        pass


# ---------------------------------------------------------------- drawing

def mic_icon(size, live=True):
    """The app icon, drawn on the same 108-unit grid as the .ico and the Android icon.
    Idle (no phone) greys the capsule, so the tray shows at a glance whether a phone is live."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    s = size / 108
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    bg = QLinearGradient(0, 0, 0, size)
    bg.setColorAt(0, QColor("#1b1e21"))
    bg.setColorAt(1, QColor("#0b0c0d"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(0, 0, size, size), 24 * s, 24 * s)
    capsule = QColor(theme.GREEN if live else "#8a9096")
    if live and size >= 32:  # the logo's glow
        glow = QRadialGradient(54 * s, 43 * s, 26 * s)
        glow.setColorAt(0, QColor(4, 250, 115, 90))
        glow.setColorAt(1, QColor(4, 250, 115, 0))
        p.setBrush(glow)
        p.drawEllipse(QRectF(28 * s, 17 * s, 52 * s, 52 * s))
    p.setBrush(capsule)
    p.drawRoundedRect(QRectF(44 * s, 26 * s, 20 * s, 34 * s), 10 * s, 10 * s)
    pen = QPen(QColor("#f2f5f3"), 5 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(36 * s, 38 * s, 36 * s, 32 * s), 180 * 16, 180 * 16)
    p.drawLine(int(54 * s), int(70 * s), int(54 * s), int(79 * s))
    p.drawLine(int(45 * s), int(80 * s), int(63 * s), int(80 * s))
    p.end()
    return pix


def app_icon(live=True):
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(mic_icon(size, live))
    return icon


def glyph_icon(name, px, colour):
    families = set(QFontDatabase.families())
    family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), "Segoe UI")
    size = px * 2  # 2x so it stays sharp on scaled displays
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    font = QFont(family)
    font.setPixelSize(int(size * 0.72))
    p.setFont(font)
    p.setPen(QColor(colour))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, GLYPH[name])
    p.end()
    return QIcon(pix)


class Style(QProxyStyle):
    """Check boxes as a rounded box with a drawn tick. (A stylesheet indicator rule would
    make them a plain square, which is why theme.py leaves indicators alone.)"""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric in (QStyle.PixelMetric.PM_IndicatorWidth, QStyle.PixelMetric.PM_IndicatorHeight):
            return 18
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            return super().drawPrimitive(element, option, painter, widget)
        on = bool(option.state & QStyle.StateFlag.State_On)
        r = QRectF(option.rect).adjusted(0.75, 0.75, -0.75, -0.75)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(P["accent"] if on else P["muted"]), 1.2))
        painter.setBrush(QColor(P["accent"] if on else P["raised"]))
        painter.drawRoundedRect(r, 5, 5)
        if on:
            tick = QPainterPath()
            tick.moveTo(r.x() + r.width() * 0.25, r.y() + r.height() * 0.52)
            tick.lineTo(r.x() + r.width() * 0.43, r.y() + r.height() * 0.70)
            tick.lineTo(r.x() + r.width() * 0.76, r.y() + r.height() * 0.31)
            pen = QPen(QColor(P["on_accent"]), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(tick)
        painter.restore()


class LevelMeter(QWidget):
    def __init__(self):
        super().__init__()
        self.value = 0.0
        self.setFixedHeight(8)

    def set_value(self, v):
        self.value = v
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(P["raised"]))
        p.drawRoundedRect(r, rad, rad)
        if self.value > 0.01:
            fill = QRectF(r.x(), r.y(), max(r.height(), r.width() * self.value), r.height())
            g = QLinearGradient(fill.topLeft(), fill.topRight())
            g.setColorAt(0, QColor(P["accent2"]))
            g.setColorAt(1, QColor(P["accent"]))
            p.setBrush(g)
            p.drawRoundedRect(fill, rad, rad)


class Dot(QWidget):
    """Status light: a glowing green when a phone is live, grey otherwise."""

    def __init__(self):
        super().__init__()
        self.live = False
        self.setFixedSize(26, 26)

    def set_live(self, live):
        if live != self.live:
            self.live = live
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        c = QRectF(self.rect()).center()
        if self.live:
            glow = QRadialGradient(c, 13)
            accent = QColor(P["accent"])
            glow.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 120))
            glow.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
            p.setBrush(glow)
            p.drawEllipse(c, 13, 13)
        p.setBrush(QColor(P["accent"] if self.live else P["muted"]))
        p.drawEllipse(c, 6, 6)


def label(text="", role=None, wrap=False):
    w = QLabel(text)
    if role:
        w.setProperty("role", role)
    w.setWordWrap(wrap)
    return w


def button(text, kind=None):
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def set_prop(widget, name, value):
    """Sets a stylesheet property and re-applies the style, which Qt doesn't do by itself."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def card(title):
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(18, 14, 18, 16)
    lay.setSpacing(10)
    lay.addWidget(label(title.upper(), "section"))
    return frame, lay


def segmented(options, current, on_change):
    frame = QFrame()
    frame.setObjectName("segment")
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(3, 3, 3, 3)
    lay.setSpacing(2)
    group = QButtonGroup(frame)
    for key, text in options:
        b = button(text, "segment")
        b.setCheckable(True)
        b.setChecked(key == current)
        b.clicked.connect(lambda _=False, k=key: on_change(k))
        group.addButton(b)
        lay.addWidget(b)
    return frame


# ---------------------------------------------------------------- demo data

class DemoEngine:
    """Stands in for receiver.Engine with made-up data (--demo), for screenshots and the smoke
    test. Nothing about the real PC, its network or the phone is ever shown."""

    PC_NAME, IPS = "MY-PC", ["192.168.1.20"]

    def __init__(self):
        self.t0 = time.monotonic()
        self.device = None
        self.usb_status = "USB: ready (1 phone connected)"

    def start(self):
        pass

    def stop(self):
        pass

    def set_device(self, name):
        self.device = name

    def set_latency(self, ms):
        pass

    def refresh_devices(self):
        pass

    def output_names(self):
        return ["CABLE Input (VB-Audio Virtual Cable)", "Speakers (High Definition Audio)",
                "Headphones (USB Audio)"]

    def resolved_output(self):
        return self.device or "CABLE Input (VB-Audio Virtual Cable)"

    def take_peak(self):
        t = time.monotonic() - self.t0
        return 0.06 + 0.05 * abs(math.sin(t * 2.3)) + 0.04 * abs(math.sin(t * 7.1))

    def snapshot(self):
        return {"source": "Pixel 8 via Wi-Fi (192.168.1.34)", "rate": 48000, "buffer_ms": 31.0,
                "loss": 0.001, "glitches": 0, "output": self.resolved_output(), "usb": self.usb_status}


# ---------------------------------------------------------------- window

class TitleBar(QFrame):
    """The app's own top bar, drawn on the window body so the two read as one surface.
    Dragging goes through startSystemMove, so snapping and multi-monitor DPI behave as usual."""

    DRAG_SLOP = 6

    def __init__(self, win):
        super().__init__(win)
        self.setObjectName("titlebar")
        self.setFixedHeight(44)
        self.win = win
        self._press = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 6, 0, 0)
        lay.setSpacing(9)
        self.crest = QLabel()
        self.crest.setPixmap(mic_icon(40).scaled(20, 20, transformMode=Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(self.crest)
        lay.addWidget(label("Micophone", "wordmark"))
        lay.addWidget(label(VERSION, "muted"))
        lay.addStretch()
        self.buttons = {}
        for key, tip, slot in (("minimize", "Minimise", win.showMinimized),
                               ("close", "Close to the tray (keeps your phone connected)", win.close)):
            b = button("", "chrome")
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(slot)
            if key == "close":
                b.setProperty("danger", "true")
            lay.addWidget(b)
            self.buttons[key] = b
        self.restyle()

    def restyle(self):
        for key, b in self.buttons.items():
            b.setIcon(glyph_icon(key, 10, P["muted"]))
            b.setIconSize(QSize(10, 10))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._press is None:
            return
        if (event.globalPosition().toPoint() - self._press).manhattanLength() < self.DRAG_SLOP:
            return
        self._press = None
        if self.win.windowHandle():
            self.win.windowHandle().startSystemMove()

    def mouseReleaseEvent(self, _event):
        self._press = None


class Window(QWidget):
    def __init__(self, engine, settings, log_queue, demo=False):
        super().__init__()
        self.engine, self.settings, self.log_queue, self.demo = engine, settings, log_queue, demo
        self.quitting = False
        self.level = 0.0
        self.live = None
        self.adb_thread = None
        self.setObjectName("root")
        self.setWindowTitle("Micophone")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowIcon(app_icon())
        self.icons = {True: app_icon(True), False: app_icon(False)}
        self._build()
        self.tray = self._build_tray()
        self.apply_theme(settings.get("theme", "system"), save=False)
        QGuiApplication.styleHints().colorSchemeChanged.connect(
            lambda _: self.settings.get("theme", "system") == "system" and self.apply_theme("system", save=False))
        self._fill_devices()
        self._check_firewall()
        # Start with focus on the window itself, not the first control (no focus ring at launch).
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setFocus()
        self._timer(50, self._fast_tick)
        self._timer(500, self._slow_tick)
        self._slow_tick()

    def _timer(self, ms, fn):
        t = QTimer(self)
        t.timeout.connect(fn)
        t.start(ms)

    # ---- layout ----------------------------------------------------------

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 4, 10, 16)
        root.setSpacing(12)
        self.title = TitleBar(self)
        root.addWidget(self.title)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 6, 0)
        body.setSpacing(12)
        root.addLayout(body)

        hero = QHBoxLayout()
        hero.setSpacing(10)
        self.dot = Dot()
        hero.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.hero = label("Waiting for your phone", "hero")
        self.sub = label("", "muted", wrap=True)
        texts.addWidget(self.hero)
        texts.addWidget(self.sub)
        hero.addLayout(texts, 1)
        body.addLayout(hero)
        self.meter = LevelMeter()
        body.addWidget(self.meter)
        self.stats = label("", "stats")
        body.addWidget(self.stats)

        out, lay = card("Output")
        row = QHBoxLayout()
        self.device = QComboBox()
        self.device.setCursor(Qt.CursorShape.PointingHandCursor)
        self.device.activated.connect(self._device_changed)
        row.addWidget(self.device, 1)
        self.refresh = button("", None)
        self.refresh.setToolTip("Look for new audio devices")
        self.refresh.setFixedWidth(40)
        self.refresh.clicked.connect(self._refresh_devices)
        row.addWidget(self.refresh)
        lay.addLayout(row)
        self.hint = label("", None, wrap=True)
        lay.addWidget(self.hint)
        self.vb_btn = button("Get VB-CABLE (free)", "primary")
        self.vb_btn.clicked.connect(lambda: webbrowser.open(VBCABLE_URL))
        lay.addWidget(self.vb_btn, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(out)

        conn, lay = card("Connect your phone")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        pc_name, ips = (DemoEngine.PC_NAME, DemoEngine.IPS) if self.demo else (rx.PC_NAME, rx.local_ips())
        self.wifi_val = label(f"Tap Find PC in the phone app, then pick {pc_name}"
                              f"{f' ({ips[0]})' if ips else ''}.", wrap=True)
        self.fw_val = label("", wrap=True)
        self.fw_btn = button("Allow", "primary")
        self.fw_btn.setToolTip("Adds a Windows Firewall rule for Micophone (asks for admin)")
        self.fw_btn.clicked.connect(self._allow_firewall)
        self.usb_val = label("", wrap=True)
        self.adb_btn = button("Install", "primary")
        self.adb_btn.setToolTip("Downloads Google's Android platform-tools (adb) for USB mode")
        self.adb_btn.clicked.connect(self._install_adb)
        for r, (key, val, btn) in enumerate((("Wi-Fi", self.wifi_val, None), ("Firewall", self.fw_val, self.fw_btn),
                                             ("USB", self.usb_val, self.adb_btn))):
            grid.addWidget(label(key, "key"), r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(val, r, 1)
            if btn:
                grid.addWidget(btn, r, 2, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        body.addWidget(conn)

        opts, lay = card("Settings")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.addWidget(label("Latency", "key"), 0, 0)
        latency = self.settings.get("latency") if self.settings.get("latency") in LATENCY else "normal"
        grid.addWidget(segmented([(k, v[0]) for k, v in LATENCY.items()], latency,
                                 self._latency_changed), 0, 1, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(label("Theme", "key"), 1, 0)
        grid.addWidget(segmented([("system", "System"), ("dark", "Dark"), ("light", "Light")],
                                 self.settings.get("theme", "system"), self.apply_theme), 1, 1,
                       Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addWidget(label("Raise latency if the voice breaks up on busy Wi-Fi.", "muted", wrap=True))
        self.autostart = QCheckBox("Start with Windows, in the tray")
        self.autostart.setCursor(Qt.CursorShape.PointingHandCursor)
        self.autostart.setChecked(False if self.demo else autostart_enabled())
        self.autostart.toggled.connect(lambda on: None if self.demo else set_autostart(on))
        lay.addWidget(self.autostart)
        body.addWidget(opts)

        log, lay = card("Activity")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(92)
        self.log.setMaximumBlockCount(200)
        lay.addWidget(self.log)
        body.addWidget(log)

        foot = QHBoxLayout()
        gh = button("github.com/farzonline/micophone", "ghost")
        gh.clicked.connect(lambda: webbrowser.open(REPO_URL))
        foot.addWidget(gh)
        foot.addStretch()
        quit_btn = button("Quit", "ghost")
        quit_btn.setToolTip("Stop Micophone completely")
        quit_btn.clicked.connect(self.quit_app)
        foot.addWidget(quit_btn)
        body.addLayout(foot)

        self.setFixedWidth(500)

    def _build_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(self.icons[False], self)
        self.tray_menu = QMenu(self)  # kept on self: an unreferenced menu is garbage-collected
        self.tray_status = self.tray_menu.addAction("Waiting for your phone")
        self.tray_status.setEnabled(False)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Open Micophone", self.show_window)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Quit Micophone", self.quit_app)
        tray.setContextMenu(self.tray_menu)
        tray.activated.connect(self._tray_activated)
        tray.setToolTip("Micophone")
        tray.show()
        return tray

    # ---- window / tray behaviour -----------------------------------------

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.hide() if self.isVisible() and not self.isMinimized() else self.show_window()

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        """Close hides to the tray: the phone stays connected. Quit (button or tray) really exits."""
        if self.quitting or not self.tray:
            self.engine.stop()
            if self.tray:
                self.tray.hide()
            event.accept()
            QApplication.instance().quit()
            return
        event.ignore()
        self.hide()
        if not self.settings.get("tray_hint_shown") and not self.demo:
            self.settings["tray_hint_shown"] = True
            save_settings(self.settings)
            self.tray.showMessage("Micophone is still running",
                                  "Your phone stays connected. Right-click the tray icon to quit.",
                                  self.icons[True], 5000)

    def quit_app(self):
        self.quitting = True
        self.close()

    def showEvent(self, event):
        super().showEvent(event)
        dwm_frame(self)

    # ---- theme -------------------------------------------------------------

    def apply_theme(self, choice, save=True):
        if save:
            self.settings["theme"] = choice
            if not self.demo:
                save_settings(self.settings)
        name = choice
        if choice == "system":
            name = "light" if QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"
        P.clear()
        P.update(theme.PALETTES[name])
        QApplication.instance().setStyleSheet(theme.stylesheet(P))
        self.title.restyle()
        self.refresh.setIcon(glyph_icon("refresh", 14, P["muted"]))
        for w in (self.dot, self.meter):
            w.update()
        if self.isVisible():
            dwm_frame(self)

    # ---- output device ---------------------------------------------------

    def _fill_devices(self):
        names = self.engine.output_names()
        saved = self.settings.get("device")
        values = [AUTO] + names + ([saved] if saved and saved not in names else [])
        self.device.clear()
        self.device.addItems(values)
        self.device.setCurrentText(saved or AUTO)
        self._update_hint(names)

    def _device_changed(self, _=None):
        name = None if self.device.currentText() == AUTO else self.device.currentText()
        self.settings["device"] = name
        if not self.demo:
            save_settings(self.settings)
        self.engine.set_device(name)
        self._update_hint(self.engine.output_names())

    def _refresh_devices(self):
        self.engine.refresh_devices()
        QTimer.singleShot(800, self._fill_devices)

    def _update_hint(self, names):
        out = self.engine.resolved_output()
        if out is None:
            text, tone, show_vb = "That output isn't connected. Plug it in and refresh, or pick another.", "warn", False
        elif out.startswith("CABLE Input"):
            text, tone, show_vb = ("Ready. In Discord, Zoom, Teams or OBS, choose “CABLE Output” "
                                   "as your microphone."), "ok", False
        else:
            has_cable = any(n.startswith("CABLE Input") for n in names)
            text = (f"Playing out loud through “{out}”, so the phone will hear it and echo. "
                    + ("Choose CABLE Input to use the phone as a microphone." if has_cable
                       else "Install VB-CABLE to use the phone as a microphone."))
            tone, show_vb = "warn", not has_cable
        self.hint.setText(text)
        set_prop(self.hint, "tone", tone)
        self.vb_btn.setVisible(show_vb)

    # ---- connection helpers ----------------------------------------------

    def _check_firewall(self):
        ok = True if self.demo else firewall_allowed()
        self.fw_val.setText("Allowed" if ok else "Wi-Fi may be blocked until you allow Micophone.")
        set_prop(self.fw_val, "tone", "ok" if ok else "warn")
        self.fw_btn.setVisible(not ok)

    def _allow_firewall(self):
        allow_firewall()
        QTimer.singleShot(3000, self._check_firewall)

    def _install_adb(self):
        self.engine.log("Downloading Android platform-tools from Google...")

        def work():
            try:
                rx.install_adb()
                self.engine.log("USB support installed.")
            except OSError as e:
                self.engine.log(f"USB support download failed: {e}")

        self.adb_thread = threading.Thread(target=work, daemon=True)
        self.adb_thread.start()

    def _latency_changed(self, key):
        self.settings["latency"] = key
        if not self.demo:
            save_settings(self.settings)
        self.engine.set_latency(LATENCY[key][1])

    # ---- refresh loops ---------------------------------------------------

    def _fast_tick(self):
        """20 Hz: level meter (dB scale, fast attack, slow release) and the activity log."""
        self.level = max(self.engine.take_peak(), self.level * 0.85)
        db = 20 * math.log10(max(self.level, 1e-4))
        self.meter.set_value(max(0.0, min(1.0, (db + 60) / 60)) if self.live else 0.0)
        while not self.log_queue.empty():
            self.log.appendPlainText(self.log_queue.get())

    def _slow_tick(self):
        s = self.engine.snapshot()
        live = bool(s["source"])
        if live:
            self.hero.setText("Live")
            self.sub.setText(s["source"])
            self.stats.setText(f"{s['rate'] / 1000:g} kHz   ·   buffer {s['buffer_ms']:.0f} ms   ·   "
                               f"packet loss {s['loss']:.1%}   ·   glitches {s['glitches']}")
        else:
            self.hero.setText("Waiting for your phone")
            self.sub.setText("Open Micophone on your phone and tap Start microphone.")
            self.stats.setText("")
        self.dot.set_live(live)
        if live != self.live:
            self.live = live
            if self.tray:
                self.tray.setIcon(self.icons[live])
        if self.tray:
            status = f"Live: {s['source']}" if live else "Waiting for your phone"
            self.tray_status.setText(status)
            self.tray.setToolTip(f"Micophone\n{status}")
        usb = s["usb"].removeprefix("USB: ")
        self.usb_val.setText(usb[:1].upper() + usb[1:])
        set_prop(self.usb_val, "tone", "ok" if usb.startswith("ready") else "")
        busy = self.adb_thread is not None and self.adb_thread.is_alive()
        self.adb_btn.setVisible("not installed" in usb or busy)
        self.adb_btn.setEnabled(not busy)
        self.adb_btn.setText("Installing..." if busy else "Install")


def rounded(pixmap, radius):
    """A window grab with Windows 11's rounded corners, for README screenshots."""
    out = QPixmap(pixmap.size())
    out.setDevicePixelRatio(pixmap.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    size = pixmap.deviceIndependentSize()
    path.addRoundedRect(QRectF(0, 0, size.width(), size.height()), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pixmap)
    p.setClipping(False)
    p.setPen(QPen(QColor(P["line"]), 1))
    p.drawPath(path)
    p.end()
    return out


def main():
    args = sys.argv[1:]
    smoke = "--smoke" in args  # CI: build the window offscreen and exit
    demo = smoke or "--demo" in args
    if smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Farzonline.Micophone")
    except (AttributeError, OSError):
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("Micophone")
    app.setQuitOnLastWindowClosed(False)  # closing hides to the tray
    app.setStyle(Style("Fusion"))

    if not demo:
        # Already running (maybe hidden in the tray)? Ask it to show itself and leave.
        probe = QLocalSocket()
        probe.connectToServer(INSTANCE_KEY)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.waitForBytesWritten(300)
            return 0

    settings = {} if demo else load_settings()
    if "--theme" in args:  # screenshots: force a theme (and show it as selected)
        settings["theme"] = args[args.index("--theme") + 1]
    log_queue = queue.SimpleQueue()

    def log(m):
        """To the window and to a log file users can attach to bug reports."""
        line = f"{time.strftime('%H:%M:%S')}  {m}"
        log_queue.put(line)
        if demo:
            return
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d')} {line}\n")
        except OSError:
            pass

    if demo:
        engine = DemoEngine()
        for line in ("09:41:02  Micophone 2.0.0 ready on port 50005.",
                     "09:41:05  Connected: Pixel 8 via Wi-Fi (192.168.1.34), 48000 Hz",
                     "09:41:05  Playing to 'CABLE Input (VB-Audio Virtual Cable)'"):
            log_queue.put(line)
    else:
        try:
            rx.APP_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.stat().st_size > 1_000_000:
                LOG_FILE.unlink()
        except OSError:
            pass
        engine = rx.Engine(device=settings.get("device"),
                           latency_ms=LATENCY.get(settings.get("latency"), LATENCY["normal"])[1], log=log)
        try:
            engine.start()
        except OSError:
            QMessageBox.critical(None, "Micophone", f"Port {rx.PORT} is used by another program, "
                                                    "so Micophone can't receive audio.")
            return 1
        log(f"Micophone {VERSION} ready on port {rx.PORT}.")
    engine.log = log

    win = Window(engine, settings, log_queue, demo)
    if not demo:
        server = QLocalServer(win)
        QLocalServer.removeServer(INSTANCE_KEY)  # a stale name left by a crash
        server.listen(INSTANCE_KEY)
        server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_window()))

    if not ("--minimized" in args and win.tray):
        win.show()
    if "--screenshot" in args:
        path = args[args.index("--screenshot") + 1]
        QTimer.singleShot(1500, lambda: (rounded(win.grab(), 8).save(path), win.quit_app()))
    if smoke:
        QTimer.singleShot(800, win.quit_app)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
