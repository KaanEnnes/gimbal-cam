"""GimbalCam masaustu uygulamasi.

Telefondaki GimbalCam'den goruntuyu alir; gimbal'i joystick/klavye ile surer,
yuzu otomatik takip eder, kamerayi uzaktan ayarlar, goruntuyu sanal kamera
olarak verir, kayit ve fotograf alir.

    python app.py
"""

import math
import os
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QKeyEvent, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from gimbalcam import FaceTracker, Phone, setup_usb

OUT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "GimbalCam")

STYLE = """
* { font-family: 'Segoe UI'; font-size: 10pt; color: #e6e8ee; }
QMainWindow, QWidget#root { background: #0f1115; }
QFrame#card { background: #171a21; border: 1px solid #232733; border-radius: 12px; }
QLabel#cardTitle { font-size: 9pt; font-weight: 600; color: #8a93a6; letter-spacing: 1px; }
QLabel#muted { color: #8a93a6; }
QPushButton {
    background: #232836; border: 1px solid #2e3446; border-radius: 8px; padding: 7px 12px;
}
QPushButton:hover { background: #2b3142; border-color: #3d4560; }
QPushButton:pressed { background: #1c2030; }
QPushButton:checked { background: #2f6bff; border-color: #2f6bff; color: white; }
QPushButton#primary { background: #2f6bff; border-color: #2f6bff; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #4a7fff; }
QPushButton#danger:checked { background: #e5484d; border-color: #e5484d; }
QLineEdit, QComboBox {
    background: #0f1115; border: 1px solid #2e3446; border-radius: 8px; padding: 6px 8px;
}
QComboBox QAbstractItemView { background: #171a21; selection-background-color: #2f6bff; }
QSlider::groove:horizontal { height: 4px; background: #2a2f3d; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #2f6bff; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }
QRadioButton::indicator, QCheckBox::indicator { width: 14px; height: 14px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #2e3446; border-radius: 4px; }
"""


def card(title):
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(8)
    t = QLabel(title.upper())
    t.setObjectName("cardTitle")
    lay.addWidget(t)
    return f, lay


class Dot(QWidget):
    """Durum isigi: yesil/sari/kirmizi, hafif nabiz animasyonlu."""

    def __init__(self, text):
        super().__init__()
        self.text = text
        self.state = "off"
        self.phase = 0.0
        self.setMinimumHeight(22)

    def set(self, state):
        self.state = state

    def tick(self):
        self.phase = (self.phase + 0.08) % (2 * math.pi)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = {"ok": QColor("#30c46b"), "wait": QColor("#f5a524"), "off": QColor("#e5484d")}[self.state]
        r = 5 + (1.5 * (1 + math.sin(self.phase)) if self.state != "ok" else 0)
        glow = QColor(col)
        glow.setAlpha(60)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(10, 11), r + 3, r + 3)
        p.setBrush(col)
        p.drawEllipse(QPointF(10, 11), 5, 5)
        p.setPen(QColor("#e6e8ee"))
        p.drawText(QRectF(24, 0, self.width() - 24, 22), Qt.AlignVCenter, self.text)


class Joystick(QWidget):
    """Surukle-birak joystick. Birakinca yumusakca merkeze doner."""

    moved = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setFixedSize(180, 180)
        self.vx = 0.0
        self.vy = 0.0
        self.dragging = False
        self.setCursor(Qt.OpenHandCursor)

    def _set_from(self, pos):
        c = QPointF(self.width() / 2, self.height() / 2)
        rad = self.width() / 2 - 26
        dx = (pos.x() - c.x()) / rad
        dy = (c.y() - pos.y()) / rad
        m = math.hypot(dx, dy)
        if m > 1:
            dx, dy = dx / m, dy / m
        self.vx, self.vy = dx, dy
        self.moved.emit(self.vx, self.vy)
        self.update()

    def mousePressEvent(self, e):
        self.dragging = True
        self.setCursor(Qt.ClosedHandCursor)
        self._set_from(e.position())

    def mouseMoveEvent(self, e):
        if self.dragging:
            self._set_from(e.position())

    def mouseReleaseEvent(self, _):
        self.dragging = False
        self.setCursor(Qt.OpenHandCursor)

    def tick(self):
        if not self.dragging and (self.vx or self.vy):
            self.vx *= 0.6
            self.vy *= 0.6
            if abs(self.vx) < 0.02 and abs(self.vy) < 0.02:
                self.vx = self.vy = 0.0
            self.moved.emit(0.0, 0.0)
            self.update()

    def value(self):
        return (self.vx, self.vy) if self.dragging else (0.0, 0.0)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        R = self.width() / 2 - 4
        g = QRadialGradient(c, R)
        g.setColorAt(0, QColor("#1d2230"))
        g.setColorAt(1, QColor("#12151c"))
        p.setBrush(g)
        p.setPen(QPen(QColor("#2e3446"), 2))
        p.drawEllipse(c, R, R)
        p.setPen(QPen(QColor("#2a3040"), 1, Qt.DashLine))
        p.drawLine(QPointF(c.x() - R + 10, c.y()), QPointF(c.x() + R - 10, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - R + 10), QPointF(c.x(), c.y() + R - 10))
        p.setPen(QColor("#56607a"))
        f = QFont("Segoe UI", 9)
        p.setFont(f)
        p.drawText(QRectF(c.x() - 10, 6, 20, 16), Qt.AlignCenter, "▲")
        p.drawText(QRectF(c.x() - 10, self.height() - 22, 20, 16), Qt.AlignCenter, "▼")
        p.drawText(QRectF(6, c.y() - 8, 16, 16), Qt.AlignCenter, "◀")
        p.drawText(QRectF(self.width() - 22, c.y() - 8, 16, 16), Qt.AlignCenter, "▶")
        rad = self.width() / 2 - 26
        k = QPointF(c.x() + self.vx * rad, c.y() - self.vy * rad)
        kg = QRadialGradient(k, 22)
        kg.setColorAt(0, QColor("#5c8dff"))
        kg.setColorAt(1, QColor("#2f6bff"))
        p.setBrush(kg)
        p.setPen(QPen(QColor(255, 255, 255, 70), 2))
        p.drawEllipse(k, 22, 22)


class VideoView(QLabel):
    """Goruntu alani. Tik = odak, tekerlek = zoom."""

    clicked = Signal(float, float)
    wheel = Signal(int)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("background: #000; border-radius: 12px;")
        self.img_rect = QRectF()
        self.focus_mark = None  # (x, y, zaman)

    def show_frame(self, frame):
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
        pm = QPixmap.fromImage(qimg).scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if self.focus_mark and time.time() - self.focus_mark[2] < 1.2:
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            age = time.time() - self.focus_mark[2]
            a = int(255 * (1 - age / 1.2))
            size = 60 - 20 * min(age / 0.3, 1)
            p.setPen(QPen(QColor(255, 210, 60, a), 2))
            fx, fy = self.focus_mark[0] * pm.width(), self.focus_mark[1] * pm.height()
            p.drawRect(QRectF(fx - size / 2, fy - size / 2, size, size))
            p.end()
        self.img_rect = QRectF((self.width() - pm.width()) / 2, (self.height() - pm.height()) / 2, pm.width(), pm.height())
        self.setPixmap(pm)

    def mousePressEvent(self, e):
        r = self.img_rect
        if r.width() and r.contains(e.position()):
            x = (e.position().x() - r.x()) / r.width()
            y = (e.position().y() - r.y()) / r.height()
            self.focus_mark = (x, y, time.time())
            self.clicked.emit(x, y)

    def wheelEvent(self, e):
        self.wheel.emit(1 if e.angleDelta().y() > 0 else -1)


def labeled_slider(lay, title, lo, hi, val, fmt, on_change):
    row = QHBoxLayout()
    lbl = QLabel(title)
    lbl.setObjectName("muted")
    v = QLabel(fmt(val))
    v.setMinimumWidth(56)
    v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    row.addWidget(lbl)
    row.addStretch()
    row.addWidget(v)
    lay.addLayout(row)
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)

    def changed(x):
        v.setText(fmt(x))
        on_change(x)

    s.valueChanged.connect(changed)
    lay.addWidget(s)
    return s


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GimbalCam")
        self.resize(1440, 860)
        self.phone = None
        self.tracker = FaceTracker()
        self.tracking = False
        self.speed = 800
        self.dead = 0.08
        self.keys = set()
        self.last_cmd = (None, 0.0)
        self.frame = None
        self.shown_id = -1
        self.vcam = None
        self.want_vcam = False
        self.writer = None
        self.rec_start = 0.0
        self.cams_filled = False
        self.zoom = 1.0
        self.settings = QSettings("kaanai", "GimbalCam")
        self.rot_offset = int(self.settings.value("rot_offset", 0))
        self.rot = 0
        self._usb_result = None

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(14, 14, 14, 14)
        h.setSpacing(14)

        # --- sol: goruntu ---
        left = QVBoxLayout()
        self.view = VideoView()
        self.view.setText("Telefon bekleniyor…")
        self.view.clicked.connect(self.focus_click)
        self.view.wheel.connect(lambda d: self.zoom_slider.setValue(self.zoom_slider.value() + d * 25))
        left.addWidget(self.view, 1)
        self.statusbar = QLabel("")
        self.statusbar.setObjectName("muted")
        left.addWidget(self.statusbar)
        h.addLayout(left, 1)

        # --- sag: paneller ---
        panel = QWidget()
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(0, 0, 6, 0)
        pv.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setFixedWidth(340)
        h.addWidget(scroll)

        # Baglanti
        c, lay = card("Bağlantı")
        mode = QHBoxLayout()
        self.rb_usb = QRadioButton("USB")
        self.rb_wifi = QRadioButton("Wi-Fi")
        self.rb_usb.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_usb)
        grp.addButton(self.rb_wifi)
        mode.addWidget(self.rb_usb)
        mode.addWidget(self.rb_wifi)
        mode.addStretch()
        lay.addLayout(mode)
        self.ip = QLineEdit()
        self.ip.setPlaceholderText("Telefon IP (ör. 192.168.1.6)")
        self.ip.setEnabled(False)
        self.rb_wifi.toggled.connect(self.ip.setEnabled)
        self.ip.setText(self.settings.value("ip", ""))
        if self.settings.value("mode", "usb") == "wifi":
            self.rb_wifi.setChecked(True)
        lay.addWidget(self.ip)
        self.btn_connect = QPushButton("Bağlan")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(self.connect_phone)
        lay.addWidget(self.btn_connect)
        self.dot_video = Dot("Görüntü")
        self.dot_ctl = Dot("Kontrol")
        self.dot_gimbal = Dot("Gimbal")
        dots = QHBoxLayout()
        for d in (self.dot_video, self.dot_ctl, self.dot_gimbal):
            dots.addWidget(d)
        lay.addLayout(dots)
        pv.addWidget(c)

        # Gimbal
        c, lay = card("Gimbal")
        jrow = QHBoxLayout()
        jrow.addStretch()
        self.joy = Joystick()
        jrow.addWidget(self.joy)
        jrow.addStretch()
        lay.addLayout(jrow)
        hint = QLabel("Sürükle  ·  ya da ok tuşları / WASD")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignCenter)
        lay.addWidget(hint)
        labeled_slider(lay, "Hız", 100, 3000, self.speed, str, lambda v: setattr(self, "speed", v))
        b = QPushButton("Ortala")
        b.clicked.connect(lambda: self.send(t="center"))
        lay.addWidget(b)
        pv.addWidget(c)

        # Takip
        c, lay = card("Yüz takibi")
        self.btn_track = QPushButton("Takibi başlat")
        self.btn_track.setCheckable(True)
        self.btn_track.toggled.connect(self.toggle_tracking)
        lay.addWidget(self.btn_track)
        labeled_slider(lay, "Ölü bölge", 2, 30, int(self.dead * 100), lambda v: f"%{v}", lambda v: setattr(self, "dead", v / 100))
        pv.addWidget(c)

        # Kamera
        c, lay = card("Kamera")
        g = QGridLayout()
        g.addWidget(QLabel("Lens"), 0, 0)
        self.cb_lens = QComboBox()
        self.cb_lens.activated.connect(lambda i: self.send(t="camera", id=self.cb_lens.itemData(i)))
        g.addWidget(self.cb_lens, 0, 1)
        g.addWidget(QLabel("Kalite"), 1, 0)
        self.cb_res = QComboBox()
        for name, w, hh, fps in (("720p · 30 fps", 1280, 720, 30), ("1080p · 30 fps", 1920, 1080, 30), ("720p · 60 fps", 1280, 720, 60), ("480p · 30 fps", 854, 480, 30)):
            self.cb_res.addItem(name, (w, hh, fps))
        self.cb_res.activated.connect(lambda i: self.send(t="res", w=self.cb_res.itemData(i)[0], h=self.cb_res.itemData(i)[1], fps=self.cb_res.itemData(i)[2]))
        g.addWidget(self.cb_res, 1, 1)
        lay.addLayout(g)
        self.zoom_slider = labeled_slider(lay, "Zoom", 100, 800, 100, lambda v: f"{v / 100:.1f}x", self.set_zoom)
        labeled_slider(lay, "Pozlama", -12, 12, 0, lambda v: f"{v:+d}", lambda v: self.send(t="ev", v=v))
        labeled_slider(lay, "Bit hızı", 1, 20, 6, lambda v: f"{v} Mbps", lambda v: self.send(t="bitrate", v=v * 1_000_000))
        row = QHBoxLayout()
        b = QPushButton("Otomatik odak")
        b.clicked.connect(lambda: self.send(t="autofocus"))
        row.addWidget(b)
        self.btn_torch = QPushButton("Fener")
        self.btn_torch.setCheckable(True)
        self.btn_torch.toggled.connect(lambda on: self.send(t="torch", v=on))
        row.addWidget(self.btn_torch)
        lay.addLayout(row)
        b = QPushButton("⟳ Görüntüyü döndür")
        b.setToolTip("Otomatik döndürme yanlışsa 90° düzelt")
        b.clicked.connect(self.rotate_offset)
        lay.addWidget(b)
        tip = QLabel("Görüntüye tıkla: odak  ·  Tekerlek: zoom")
        tip.setObjectName("muted")
        lay.addWidget(tip)
        pv.addWidget(c)

        # Cikti
        c, lay = card("Çıktı")
        self.btn_vcam = QPushButton("Sanal kamera")
        self.btn_vcam.setCheckable(True)
        self.btn_vcam.toggled.connect(lambda on: setattr(self, "want_vcam", on))
        lay.addWidget(self.btn_vcam)
        row = QHBoxLayout()
        self.btn_rec = QPushButton("● Kayıt")
        self.btn_rec.setObjectName("danger")
        self.btn_rec.setCheckable(True)
        self.btn_rec.toggled.connect(self.toggle_record)
        row.addWidget(self.btn_rec)
        b = QPushButton("Fotoğraf")
        b.clicked.connect(self.snapshot)
        row.addWidget(b)
        lay.addLayout(row)
        self.out_info = QLabel(f"Kayıtlar: {OUT_DIR}")
        self.out_info.setObjectName("muted")
        self.out_info.setWordWrap(True)
        lay.addWidget(self.out_info)
        pv.addWidget(c)
        pv.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.loop)
        self.timer.start(16)
        self.setFocusPolicy(Qt.StrongFocus)
        QTimer.singleShot(300, self.connect_phone)

    # ---------- baglanti ----------
    def connect_phone(self):
        if self.phone:
            self.phone.close()
            self.phone = None
        self.settings.setValue("mode", "wifi" if self.rb_wifi.isChecked() else "usb")
        if self.rb_wifi.isChecked():
            host = self.ip.text().strip()
            if not host:
                self.statusbar.setText("Wi-Fi için telefonun IP adresini yaz (telefon ekranının sol üstünde).")
                return
            self.settings.setValue("ip", host)
            self._start_phone(host)
        else:
            # adb yavas/takili olabilir: arayuz donmasin diye arka planda bul
            self.statusbar.setText("Telefon aranıyor (adb)…")
            self.btn_connect.setEnabled(False)
            self._usb_result = None

            def work():
                self._usb_result = setup_usb() or ""

            threading.Thread(target=work, daemon=True).start()

    def _start_phone(self, host):
        self.phone = Phone(host)
        self.cams_filled = False
        self.statusbar.setText(f"Bağlanılıyor: {host}")

    def send(self, **msg):
        if self.phone:
            self.phone.send(**msg)

    def set_zoom(self, v):
        self.zoom = v / 100
        self.send(t="zoom", v=self.zoom)

    def rotate_offset(self):
        self.rot_offset = (self.rot_offset + 90) % 360
        self.settings.setValue("rot_offset", self.rot_offset)

    def focus_click(self, x, y):
        # Ekrandaki (donmus) noktayi kameranin kendi koordinatina cevir
        r = self.rot
        if r == 90:
            x, y = y, 1 - x
        elif r == 180:
            x, y = 1 - x, 1 - y
        elif r == 270:
            x, y = 1 - y, x
        self.send(t="focus", x=x, y=y)

    def toggle_tracking(self, on):
        self.tracking = on
        self.tracker.box = None
        self.btn_track.setText("Takibi durdur" if on else "Takibi başlat")

    # ---------- kayit ----------
    def toggle_record(self, on):
        if on:
            if self.frame is None:
                self.btn_rec.setChecked(False)
                return
            os.makedirs(OUT_DIR, exist_ok=True)
            h, w = self.frame.shape[:2]
            path = os.path.join(OUT_DIR, datetime.now().strftime("video_%Y%m%d_%H%M%S.mp4"))
            fps = (self.phone.status.get("fps") if self.phone else None) or 30
            self.writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            self.rec_size = (w, h)
            self.rec_start = time.time()
            self.out_info.setText(f"Kaydediliyor: {path}")
        else:
            if self.writer:
                self.writer.release()
                self.writer = None
            self.btn_rec.setText("● Kayıt")
            self.out_info.setText(f"Kayıt bitti. Klasör: {OUT_DIR}")

    def snapshot(self):
        if self.frame is None:
            return
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, datetime.now().strftime("foto_%Y%m%d_%H%M%S.jpg"))
        cv2.imwrite(path, self.frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        self.out_info.setText(f"Fotoğraf: {path}")

    # ---------- klavye ----------
    KEYMAP = {Qt.Key_Left: "L", Qt.Key_A: "L", Qt.Key_Right: "R", Qt.Key_D: "R",
              Qt.Key_Up: "U", Qt.Key_W: "U", Qt.Key_Down: "D", Qt.Key_S: "D"}

    def keyPressEvent(self, e: QKeyEvent):
        k = self.KEYMAP.get(e.key())
        if k:
            self.keys.add(k)
        elif e.key() == Qt.Key_T and not e.isAutoRepeat():
            self.btn_track.toggle()
        elif e.key() == Qt.Key_C:
            self.send(t="center")
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e: QKeyEvent):
        if e.isAutoRepeat():
            return
        k = self.KEYMAP.get(e.key())
        if k:
            self.keys.discard(k)

    # ---------- ana dongu ----------
    def loop(self):
        for d in (self.dot_video, self.dot_ctl, self.dot_gimbal):
            d.tick()
        self.joy.tick()
        if self._usb_result is not None:
            host, self._usb_result = self._usb_result, None
            self.btn_connect.setEnabled(True)
            if host:
                self._start_phone(host)
            else:
                self.statusbar.setText("USB/kablosuz adb ile telefon bulunamadı. Wi-Fi seçip telefonun IP'sini yaz.")
        ph = self.phone
        if ph is None:
            for d in (self.dot_video, self.dot_ctl, self.dot_gimbal):
                d.set("off")
            return

        st = ph.status
        self.dot_video.set("ok" if ph.video_ok and ph.frame is not None else ("wait" if ph.video_ok else "off"))
        self.dot_ctl.set("ok" if ph.control_ok else "off")
        self.dot_gimbal.set("ok" if st.get("gimbalConnected") else ("wait" if ph.control_ok else "off"))

        if st.get("cameras") and not self.cams_filled:
            self.cb_lens.clear()
            for c in st["cameras"]:
                cid, facing = c.split(":")
                self.cb_lens.addItem(f"{facing.capitalize()} kamera ({cid})", cid)
            self.cams_filled = True
        if st.get("camera") is not None:
            i = self.cb_lens.findData(st.get("camera"))
            if i >= 0 and i != self.cb_lens.currentIndex():
                self.cb_lens.setCurrentIndex(i)

        new = ph.frame_id != self.shown_id and ph.frame is not None
        if new:
            self.shown_id = ph.frame_id
            # Telefon dikey/yan donunce goruntuyu duz tut (on kamerada yon ters)
            sign = -1 if st.get("front") else 1
            self.rot = (sign * int(st.get("rot", 0)) + self.rot_offset) % 360
            f0 = ph.frame
            if self.rot == 90:
                f0 = cv2.rotate(f0, cv2.ROTATE_90_CLOCKWISE)
            elif self.rot == 180:
                f0 = cv2.rotate(f0, cv2.ROTATE_180)
            elif self.rot == 270:
                f0 = cv2.rotate(f0, cv2.ROTATE_90_COUNTERCLOCKWISE)
            self.frame = f0

        # --- gimbal komutu ---
        now = time.time()
        jx, jy = self.joy.value()
        kx = (1 if "R" in self.keys else 0) - (1 if "L" in self.keys else 0)
        ky = (1 if "U" in self.keys else 0) - (1 if "D" in self.keys else 0)
        manual = bool(jx or jy or kx or ky)
        if manual:
            mx = int((jx + kx) * self.speed)
            my = int((jy + ky) * self.speed)
        elif self.tracking and self.frame is not None and new:
            self.tracker_dead(self.dead)
            mx, my = self.tracker.update(self.frame, self.speed)
            if st.get("front"):
                # Gimbal telefonu yukari egince on kamera asagi bakar: dikey yon ters
                my = -my
        elif self.tracking:
            mx, my = self.last_cmd[0] or (0, 0)
        else:
            mx = my = 0
        cmd = (mx, my)
        if cmd != self.last_cmd[0] or (cmd != (0, 0) and now - self.last_cmd[1] > 0.1):
            self.send(t="move", x=mx, y=my)
            self.last_cmd = (cmd, now)

        if not new:
            return

        # --- sanal kamera ---
        f = self.frame
        fh, fw = f.shape[:2]
        if self.want_vcam and self.vcam is None:
            try:
                import pyvirtualcam
                self.vcam = pyvirtualcam.Camera(width=fw, height=fh, fps=30, fmt=pyvirtualcam.PixelFormat.BGR)
                self.out_info.setText(f"Sanal kamera açık: {self.vcam.device}")
            except Exception as ex:
                self.out_info.setText(f"Sanal kamera açılamadı: OBS Studio kurulu olmalı.\n({ex})")
                self.btn_vcam.setChecked(False)
        if self.vcam is not None:
            if not self.want_vcam or (fw, fh) != (self.vcam.width, self.vcam.height):
                self.vcam.close()
                self.vcam = None
            else:
                self.vcam.send(f)

        # --- kayit ---
        if self.writer is not None:
            if (fw, fh) == self.rec_size:
                self.writer.write(f)
            secs = int(now - self.rec_start)
            self.btn_rec.setText(f"■ {secs // 60:02d}:{secs % 60:02d}")

        # --- ekran ---
        disp = f
        if self.tracking and self.tracker.box is not None:
            disp = f.copy()
            x, y, bw, bh = self.tracker.box
            cv2.rectangle(disp, (x, y), (x + bw, y + bh), (90, 220, 120), 3)
        self.view.show_frame(disp)
        gs = st.get("gimbal", "?")
        self.statusbar.setText(
            f"{fw}×{fh}  ·  {ph.fps:.0f} fps  ·  Gimbal: {gs} (kopma {st.get('drops', 0)}, sebep {st.get('dropReason', '-')})  ·  Takip: {'açık' if self.tracking else 'kapalı'}"
        )

    def tracker_dead(self, dead):
        self.tracker.dead = dead

    def closeEvent(self, e):
        self.send(t="stop")
        if self.writer:
            self.writer.release()
        if self.vcam:
            self.vcam.close()
        if self.phone:
            self.phone.close()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
