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
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeyEvent, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QSystemTrayIcon, QRadioButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from gimbalcam import AudioPlayer, FaceTracker, Phone, find_phone_on_lan, port_open, resource, setup_usb
from room import QuickTunnel, RoomDirectory, RoomServer

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


class AxisMap:
    """Gimbal komut eksenleri (x, y) ile goruntudeki yon (sag, yukari) arasindaki esleme.

    Gimbal dikey moddayken veya on kamerada eksenler ters ya da yer degistirmis olabilir;
    bu esleme kalibrasyonla (gimbal'i kisaca oynatip goruntunun kaymasini olcerek) bulunur."""

    def __init__(self, h=("x", 1), v=("y", 1)):
        self.h = h
        self.v = v

    def to_cmd(self, right, up):
        c = {"x": 0.0, "y": 0.0}
        c[self.h[0]] += right * self.h[1]
        c[self.v[0]] += up * self.v[1]
        return int(c["x"]), int(c["y"])


def default_map(rot, front):
    """Olculmus varsayilanlar (arka kamera, M1). Dikey modda (90) eksenler yer degistirmiyor,
    sadece dikey motor ters calisiyor. On kamerada dikey yon ayrica tersine doner."""
    if rot in (90, 270):
        m = AxisMap(("x", 1), ("y", -1))
    elif rot == 180:
        m = AxisMap(("x", -1), ("y", -1))
    else:
        m = AxisMap(("x", 1), ("y", 1))
    if front:
        m = AxisMap(m.h, (m.v[0], -m.v[1]))
    return m


def _cal_gray(frame):
    h, w = frame.shape[:2]
    sc = 320 / w
    g = cv2.cvtColor(cv2.resize(frame, (320, int(h * sc))), cv2.COLOR_BGR2GRAY)
    return np.float32(g), sc


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
        self.maps = {}  # (kamera, yon) -> AxisMap
        self.note = ("", 0.0)
        self.room = None
        self.tunnel = None
        self.player = AudioPlayer()
        self.talk_player = AudioPlayer()  # odadaki kisinin bas-konus sesi (PC hoparloru)
        self.wake_wanted_at = 0.0     # sarjdayken biri kullanmak istedi
        self.tilt_s = 0.0             # ufuk kilidi: yumusatilmis egiklik
        self.last_wants_sent = 0.0    # telefona "ses/egim istiyorum" bildirimi
        self.last_tilt_push = 0.0
        self.last_activity = time.time()
        self.last_charge_hint = 0.0
        self.was_charging = None
        self._audio_phone = None
        self.room_last_frame = 0.0
        self.viewer_active_until = 0.0
        self.cal = None  # devam eden kalibrasyon
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
        scroll.setFixedWidth(352)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        panel.setFixedWidth(336)
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
        b = QPushButton("Kablosuz eşleştir (QR)")
        b.setToolTip("Kablosuz hata ayıklamayı QR koduyla eşleştir; kablo gerekmez")
        b.clicked.connect(self.open_pairing)
        lay.addWidget(b)
        self.chk_smooth = QCheckBox("Akıcı görüntü (+0.2 sn gecikme)")
        self.chk_smooth.setToolTip("Wi-Fi gecikme dalgalanmasından gelen takılmaları gizler. Kapalı: en düşük gecikme.")
        self.chk_smooth.setChecked(self.settings.value("smooth", "true") == "true")
        self.chk_smooth.toggled.connect(lambda v: self.settings.setValue("smooth", "true" if v else "false"))
        lay.addWidget(self.chk_smooth)
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
        self.speed_slider = labeled_slider(lay, "Hız", 100, 1500, self.speed, str, lambda v: setattr(self, "speed", v))
        b = QPushButton("Ortala")
        b.clicked.connect(lambda: self.send(t="center"))
        lay.addWidget(b)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Mod"))
        self.cb_mode = QComboBox()
        for name in ("Takip (yatay + dikey)", "Yatay takip", "Kilitli (tüm eksenler)", "POV"):
            self.cb_mode.addItem(name)
        self.cb_mode.setToolTip("Gimbal'ın kendi çalışma modu. Kilitli: sapı çevirsen de kamera aynı yöne bakar.")
        self.cb_mode.activated.connect(lambda i: self.send(t="gmode", v=i + 1))
        mrow.addWidget(self.cb_mode, 1)
        lay.addLayout(mrow)
        self.batt = QLabel("Pil: -")
        self.batt.setObjectName("muted")
        lay.addWidget(self.batt)
        self.chk_sleep = QCheckBox("Şarjdayken uyku (hareket etme)")
        self.chk_sleep.setChecked(self.settings.value("sleep_guard", "true") == "true")
        self.chk_sleep.setToolTip("Gimbal şarja takılıyken motorları zorlamaz; kullanmak için kabloyu çıkarman istenir")
        self.chk_sleep.toggled.connect(lambda v: self.settings.setValue("sleep_guard", "true" if v else "false"))
        lay.addWidget(self.chk_sleep)
        pv.addWidget(c)

        # Takip
        c, lay = card("Yüz takibi")
        self.btn_track = QPushButton("Takibi başlat")
        self.btn_track.setCheckable(True)
        self.btn_track.toggled.connect(self.toggle_tracking)
        lay.addWidget(self.btn_track)
        labeled_slider(lay, "Ölü bölge", 2, 30, int(self.dead * 100), lambda v: f"%{v}", lambda v: setattr(self, "dead", v / 100))
        b = QPushButton("Yönleri yeniden ölç")
        b.setToolTip("Gimbal'ı kısaca sağa ve yukarı oynatıp görüntünün hangi yöne kaydığını ölçer")
        b.clicked.connect(lambda: self.start_calibration(force=True))
        lay.addWidget(b)
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
        self.ev_slider = labeled_slider(lay, "Pozlama", -12, 12, 0, lambda v: f"{v:+d}", lambda v: self.send(t="ev", v=v))
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
        self.chk_hevc = QCheckBox("HEVC (aynı hızda daha temiz görüntü)")
        self.chk_hevc.setToolTip("H.265 kodlama: aynı bant genişliğinde yaklaşık iki kat temiz. Oda açıkken otomatik H.264'e döner (tarayıcılar HEVC çözemez).")
        self.chk_hevc.setChecked(self.settings.value("hevc", "false") == "true")
        self.chk_hevc.toggled.connect(self.toggle_hevc)
        lay.addWidget(self.chk_hevc)
        self.chk_horizon = QCheckBox("Ufuk kilidi (görüntüyü hep düz tut)")
        self.chk_horizon.setChecked(self.settings.value("horizon", "false") == "true")
        self.chk_horizon.setToolTip("Telefonun eğimini ölçüp görüntüyü ters yönde çevirir ve hafifçe kırpar")
        self.chk_horizon.toggled.connect(lambda v: self.settings.setValue("horizon", "true" if v else "false"))
        lay.addWidget(self.chk_horizon)
        b = QPushButton("⟳ Görüntüyü döndür")
        b.setToolTip("Otomatik döndürme yanlışsa 90° düzelt")
        b.clicked.connect(self.rotate_offset)
        lay.addWidget(b)
        tip = QLabel("Görüntüye tıkla: odak  ·  Tekerlek: zoom")
        tip.setObjectName("muted")
        lay.addWidget(tip)
        pv.addWidget(c)

        # Oda
        c, lay = card("Oda (uzaktan izleme)")
        self.room_name = QLineEdit()
        self.room_name.setPlaceholderText("Oda adı (listede görünür)")
        self.room_name.setText(self.settings.value("room_name", "Kaan'ın odası"))
        lay.addWidget(self.room_name)
        self.room_pass = QLineEdit()
        self.room_pass.setPlaceholderText("Oda şifresi (en az 4 karakter)")
        self.room_pass.setEchoMode(QLineEdit.Password)
        self.room_pass.setText(self.settings.value("room_pass", ""))
        self.chk_autopass = QCheckBox("Şifreyi otomatik oluştur")
        self.chk_autopass.setToolTip("Her oda kurulduğunda yeni, 6 haneli rastgele şifre")
        self.chk_autopass.setChecked(self.settings.value("auto_pass", "true") == "true")
        self.chk_autopass.toggled.connect(lambda v: (self.settings.setValue("auto_pass", "true" if v else "false"), self.room_pass.setVisible(not v)))
        self.room_pass.setVisible(not self.chk_autopass.isChecked())
        lay.addWidget(self.chk_autopass)
        lay.addWidget(self.room_pass)
        self.btn_room = QPushButton("Oda kur")
        self.btn_room.setObjectName("primary")
        self.btn_room.setCheckable(True)
        self.btn_room.toggled.connect(self.toggle_room)
        lay.addWidget(self.btn_room)
        self.room_code = QLabel("")
        self.room_code.setAlignment(Qt.AlignCenter)
        self.room_code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.room_code.setStyleSheet("font-size:13pt; font-weight:600; color:#5c8dff;")
        lay.addWidget(self.room_code)
        self.room_pw_label = QLabel("")
        self.room_pw_label.setAlignment(Qt.AlignCenter)
        self.room_pw_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.room_pw_label.setStyleSheet("font-size:15pt; font-weight:700; letter-spacing:3px; color:#e6e8ee;")
        lay.addWidget(self.room_pw_label)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("Bas-konuş sesi"))
        self.cb_talk_out = QComboBox()
        self.cb_talk_out.addItem("PC hoparlörü", "pc")
        self.cb_talk_out.addItem("Telefon hoparlörü", "phone")
        i = self.cb_talk_out.findData(self.settings.value("talk_out", "pc"))
        self.cb_talk_out.setCurrentIndex(max(0, i))
        self.cb_talk_out.currentIndexChanged.connect(lambda _: self.settings.setValue("talk_out", self.cb_talk_out.currentData()))
        trow.addWidget(self.cb_talk_out, 1)
        lay.addLayout(trow)
        self.btn_copy = QPushButton("Daveti kopyala (link + şifre)")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self.copy_room_link)
        lay.addWidget(self.btn_copy)
        self.room_info = QLabel("Odayı kurunca kod ve link burada görünür. Katılan kişi kaanai.site/gimbal → Odaya katıl.")
        self.room_info.setObjectName("muted")
        self.room_info.setWordWrap(True)
        lay.addWidget(self.room_info)
        pv.addWidget(c)

        # Cikti
        c, lay = card("Çıktı")
        row = QHBoxLayout()
        self.btn_sound = QPushButton("🔊 Sesi PC'de çal")
        self.btn_sound.setCheckable(True)
        self.btn_sound.setToolTip("Telefon mikrofonunu bilgisayarın hoparlöründen çal (telefon yanındaysa yankı yapabilir)")
        self.btn_sound.toggled.connect(self.toggle_sound)
        row.addWidget(self.btn_sound)
        lay.addLayout(row)
        labeled_slider(lay, "Ses düzeyi", 0, 200, 100, lambda v: f"%{v}", lambda v: setattr(self.player, "volume", v / 100))
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
        icon_path = resource("icon.png")
        self.setWindowIcon(QIcon(icon_path))
        self.tray = QSystemTrayIcon(QIcon(icon_path), self)
        self.tray.setToolTip("GimbalCam")
        self.tray.show()
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
            # Modem telefona yeni IP vermis olabilir: kayitli adres cevap vermezse agda ara
            self.statusbar.setText(f"Bağlanılıyor: {host}…")
            self.btn_connect.setEnabled(False)
            self._usb_result = None

            def work():
                if port_open(host):
                    self._usb_result = host
                    return
                found = find_phone_on_lan()
                self._usb_result = found or host

            threading.Thread(target=work, daemon=True).start()
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

    # ---------- oda ----------
    def toggle_room(self, on):
        if on:
            if self.chk_autopass.isChecked():
                import secrets
                pw = f"{secrets.randbelow(1_000_000):06d}"
                self.room_pass.setText(pw)
            pw = self.room_pass.text()
            if len(pw) < 4:
                self.room_info.setText("Şifre en az 4 karakter olmalı.")
                self.btn_room.setChecked(False)
                return
            self.settings.setValue("room_pass", pw)
            self.settings.setValue("room_name", self.room_name.text().strip() or "GimbalCam odası")
            self.directory = None
            self.room = RoomServer(pw)
            self.room.on_talk = self._on_talk
            port = self.room.start()
            self.tunnel = QuickTunnel(port)
            self.tunnel.start()
            self._room_phone = None
            self.btn_room.setText("Odayı kapat")
            self.last_wants_sent = 0.0  # HEVC aciksa hemen H.264'e gec (tarayicilar icin)
            self.room_pass.setEnabled(False)
            self.room_code.setText("Oda açılıyor…")
            self.room_info.setText("İnternete açılıyor (5-15 sn).")
        else:
            if getattr(self, "directory", None):
                self.directory.close()
                self.directory = None
            if self.tunnel:
                self.tunnel.stop()
            if self.room:
                if self.phone and self.room.push_packet in self.phone.packet_listeners:
                    self.phone.packet_listeners.remove(self.room.push_packet)
                self.room.stop()
            self.room = self.tunnel = None
            self.last_wants_sent = 0.0  # oda kapandi: HEVC secildiyse hemen geri don
            self.btn_room.setText("Oda kur")
            self.room_pass.setEnabled(True)
            self.room_code.setText("")
            self.room_pw_label.setText("")
            self.btn_copy.setEnabled(False)
            self.room_info.setText("Oda kapalı.")

    def room_link(self):
        d = getattr(self, "directory", None)
        if d and d.id:
            return f"https://kaanai.site/gimbal/oda/#{d.id}"
        return f"https://kaanai.site/gimbal/oda/#{self.tunnel.code}" if self.tunnel and self.tunnel.code else ""

    def copy_room_link(self):
        link = self.room_link()
        if link:
            QApplication.clipboard().setText(f"GimbalCam odasına katıl: {link}\nŞifre: {self.room.password}")
            self.set_note("Davet kopyalandı: link + şifre. Yapıştırıp gönderebilirsin.")

    def room_tick(self, now):
        r, t = self.room, self.tunnel
        if not r or not t:
            return
        if t.url and getattr(self, "directory", None) is None:
            self.directory = RoomDirectory(self.room_name.text().strip() or "GimbalCam odası", t.url,
                                           lambda: len(r.viewers))
        d = getattr(self, "directory", None)
        if d and d.id and not self.room_code.text().endswith(d.id):
            self.room_code.setText(f"Oda kodu: {d.id}")
        if t.url and not self.btn_copy.isEnabled():
            self.room_code.setText(f"Oda kodu: {t.code}")
            self.room_pw_label.setText(f"Şifre: {self.room.password}")
            self.btn_copy.setEnabled(True)
        if t.error and not t.url:
            self.room_info.setText(f"Oda internete açılamadı: {t.error}")
        if t.url:
            names = r.viewer_names
            self.room_info.setText(
                ("İzleyenler: " + ", ".join(names)) if names else "Oda açık. Henüz kimse bağlanmadı."
            )
        # Telefon nesnesi degistiyse (yeniden baglanma) H.264 dinleyicisini tasi
        if self.phone is not self._room_phone:
            if self._room_phone and r.push_packet in self._room_phone.packet_listeners:
                self._room_phone.packet_listeners.remove(r.push_packet)
            if self.phone:
                # Oda baglantidan sonra acildiysa ilk SPS/PPS'i kacirdi: sakli olani ver (yoksa "Goruntu bekleniyor")
                if self.phone.last_config:
                    r.push_packet(*self.phone.last_config)
                self.phone.packet_listeners.append(r.push_packet)
                self.send(t="keyframe")
            self._room_phone = self.phone
        st = self.phone.status if self.phone else {}
        talking = r.talking
        if talking:
            self.set_note(f"🎤 {talking} konuşuyor")
        r.set_info(
            talking=talking,
            tracking=self.tracking, rot=self.rot, speed=self.speed, zoom=self.zoom,
            ev=self.ev_slider.value(), torch=self.btn_torch.isChecked(),
            camera=st.get("camera"), cameras=st.get("cameras", []), res=self.cb_res.currentIndex(),
            resList=[self.cb_res.itemText(i) for i in range(self.cb_res.count())],
            gimbal=st.get("gimbal", ""), calibrating=self.cal is not None,
            gmode=self.cb_mode.currentIndex() + 1, horizon=self.chk_horizon.isChecked(),
        )
        for c in r.pop_commands():
            self.remote_command(c)
        # Izleyici kare kacirdiysa 3 sn'lik dogal anahtar kareyi bekletme: telefondan hemen iste (en fazla 1/sn)
        if r.key_wanted and now - getattr(self, "_last_key_req", 0) > 1.0:
            r.key_wanted = False
            self._last_key_req = now
            self.send(t="keyframe")
        if self.chk_horizon.isChecked() and now - self.last_tilt_push > 1 / 15:
            self.last_tilt_push = now
            r.push_tilt(self.tilt_s)
        # JPEG yalnizca WebCodecs'i olmayan izleyiciler icin (~15 fps)
        if r.wants_jpeg and self.frame is not None and now - self.room_last_frame >= 1 / 15:
            self.room_last_frame = now
            f = self.frame
            h, w = f.shape[:2]
            if max(w, h) > 960:
                sc = 960 / max(w, h)
                f = cv2.resize(f, (int(w * sc), int(h * sc)))
            ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 72])
            if ok:
                r.push_frame(buf.tobytes())

    def _on_talk(self, pcm, name):
        # Oda is parcaciginda: izleyicinin sesi PC'den ya da telefondan calinir
        if self.settings.value("talk_out", "pc") == "pc":
            if not self.talk_player.stream:
                self.talk_player.start()
            self.talk_player.feed(pcm)
            # Telefon mikrofonu da kisa sure sussun: PC'den cikan ses Asu'ya yanki olarak donmesin
            self.send(t="duck")
        else:
            import base64
            self.send(t="talk", pcm=base64.b64encode(pcm).decode())

    def remote_command(self, d):
        """Odadaki izleyicinin komutu. Ayni arayuz elemanlari uzerinden uygulanir (PC ekrani da guncellenir)."""
        c = d.get("c")

        def num(k, lo, hi, default=0):
            try:
                return max(lo, min(hi, float(d.get(k, default))))
            except (TypeError, ValueError):
                return default

        if c == "track":
            self.btn_track.setChecked(bool(d.get("v")))
        elif c == "center":
            self.send(t="center")
        elif c == "zoom":
            self.zoom_slider.setValue(int(num("v", 1, 8, 1) * 100))
        elif c == "ev":
            self.ev_slider.setValue(int(num("v", -12, 12)))
        elif c == "torch":
            self.btn_torch.setChecked(bool(d.get("v")))
        elif c == "autofocus":
            self.send(t="autofocus")
        elif c == "focus":
            self.view.focus_mark = (num("x", 0, 1, .5), num("y", 0, 1, .5), time.time())
            self.focus_click(num("x", 0, 1, .5), num("y", 0, 1, .5))
        elif c == "camera":
            i = self.cb_lens.findData(str(d.get("id")))
            if i >= 0:
                self.cb_lens.setCurrentIndex(i)
                self.send(t="camera", id=self.cb_lens.itemData(i))
        elif c == "res":
            i = int(num("v", 0, self.cb_res.count() - 1))
            self.cb_res.setCurrentIndex(i)
            w, h, fps = self.cb_res.itemData(i)
            self.send(t="res", w=w, h=h, fps=fps)
        elif c == "speed":
            self.speed_slider.setValue(int(num("v", 100, 1500, 800)))
        elif c == "recal":
            self.start_calibration(force=True)
        elif c == "gmode":
            i = int(num("v", 1, 4, 1))
            self.cb_mode.setCurrentIndex(i - 1)
            self.send(t="gmode", v=i)
        elif c == "horizon":
            self.chk_horizon.setChecked(bool(d.get("v")))
        else:
            return
        self.set_note(f"{d.get('_from', 'Misafir')}: {c}")

    def open_pairing(self):
        from kablosuz_eslestir import PairWindow
        self.pair_win = PairWindow()
        self.pair_win.show()

    def notify(self, title, text):
        self.set_note(text)
        try:
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 6000)
        except Exception:
            pass

    def charge_tick(self, now, charging, st):
        bat = st.get("gBattery", -1)
        if bat is None or bat < 0:
            self.batt.setText("Pil: bilgi yok")
        else:
            lvl = max(0, min(4, int(bat)))
            bars = "▮" * lvl + "▯" * (4 - lvl)
            state = "⚡ şarjda" + (" · uyku" if self.chk_sleep.isChecked() else "") if charging else "pilde"
            self.batt.setText(f"Pil: {bars}  ({state})")
        if self.was_charging is True and not charging:
            if now - self.wake_wanted_at < 120:
                self.notify("Gimbal uyandı", "Şarj çıkarıldı, kontrol açık.")
            self.wake_wanted_at = 0.0
        self.was_charging = charging
        # Uzun suredir kullanilmiyor ve pil dusukse sarja takmayi hatirlat (saatte bir)
        if (not charging and bat is not None and 0 <= bat <= 1 and now - self.last_activity > 600
                and now - self.last_charge_hint > 3600 and not (self.room and self.room.viewers)):
            self.notify("Gimbal boşta", "Bir süredir kullanılmıyor ve pil düşük; şarja takabilirsin.")
            self.last_charge_hint = now
        if self.room:
            self.room.set_info(gCharging=charging, gBattery=bat, sleeping=charging and self.chk_sleep.isChecked(),
                               wakeWanted=now - self.wake_wanted_at < 30)

    def phone_tracks(self, st):
        return bool(st.get("faceDetect"))

    def send_track_params(self, st):
        """Takip telefondaysa ayarlari (olculmus eksen eslemesi, goruntu yonu, hiz) telefona bildir."""
        if not self.phone_tracks(st):
            return
        key = (st.get("camera"), self.rot)
        on = self.tracking and self.cal is None and key in self.maps
        amap = self.maps.get(key) or default_map(self.rot, bool(st.get("front")))
        self.send(t="track", on=on, rot=self.rot, h=amap.h[0], hs=amap.h[1], va=amap.v[0], vs=amap.v[1],
                  speed=self.speed, dead=self.dead, sleep=self.chk_sleep.isChecked())

    def toggle_hevc(self, on):
        self.settings.setValue("hevc", "true" if on else "false")
        self.last_wants_sent = 0.0  # hemen telefona bildir

    def toggle_sound(self, on):
        if on and not self.player.start():
            self.btn_sound.setChecked(False)
            self.set_note("Ses çıkışı açılamadı.")
        elif not on:
            self.player.stop()

    def _on_audio(self, pcm):
        # Telefonun video is parcaciginda calisir
        if self.btn_sound.isChecked():
            self.player.feed(pcm)
        r = self.room
        if r:
            r.push_audio(pcm)

    def set_note(self, text):
        self.note = (text, time.time())

    # ---------- eksen kalibrasyonu ----------
    CAL_SPEED = 300

    def start_calibration(self, force=False):
        if self.phone is None or self.frame is None:
            return
        key = (self.phone.status.get("camera"), self.rot)
        if force:
            self.maps.pop(key, None)
        self.cal = {"key": key, "axes": ["x", "y"], "res": {}, "step": "start", "t": 0.0}
        self.set_note("Yönler ölçülüyor: gimbal kısaca sağa ve yukarı oynayacak…")

    def step_calibration(self, now, new_frame):
        c = self.cal
        if not c["axes"]:
            self.finish_calibration()
            return
        a = c["axes"][0]
        step = c["step"]
        if step == "start":
            c["f0"], c["sc"] = _cal_gray(self.frame)
            self.send(t="move", **{"x": self.CAL_SPEED if a == "x" else 0, "y": self.CAL_SPEED if a == "y" else 0})
            c["step"], c["t"] = "moving", now
        elif step == "moving":
            if now - c["t"] >= 0.1:
                # Telefon 500 ms komut gelmeyince durur; hareket boyunca tekrarla
                self.send(t="move", **{"x": self.CAL_SPEED if a == "x" else 0, "y": self.CAL_SPEED if a == "y" else 0})
            if now - c["t"] >= 0.3:
                self.send(t="move", x=0, y=0)
                c["step"], c["t"] = "settle", now
        elif step == "settle":
            if now - c["t"] >= 0.5 and new_frame:
                f1, _ = _cal_gray(self.frame)
                f0 = c["f0"]
                if f1.shape == f0.shape:
                    win = cv2.createHanningWindow((f0.shape[1], f0.shape[0]), cv2.CV_32F)
                    (dx, dy), conf = cv2.phaseCorrelate(f0, f1, win)
                    # Icerik sola kayiyorsa gorus saga donmustur. Dusuk guvenli olcumu kullanma.
                    if conf >= 0.2:
                        c["res"][a] = (-dx, dy)
                self.send(t="move", **{"x": -self.CAL_SPEED if a == "x" else 0, "y": -self.CAL_SPEED if a == "y" else 0})
                c["step"], c["t"] = "back", now
        elif step == "back":
            if now - c["t"] >= 0.1:
                self.send(t="move", **{"x": -self.CAL_SPEED if a == "x" else 0, "y": -self.CAL_SPEED if a == "y" else 0})
            if now - c["t"] >= 0.3:
                self.send(t="move", x=0, y=0)
                c["step"], c["t"] = "rest", now
        elif step == "rest":
            if now - c["t"] >= 0.4:
                c["axes"].pop(0)
                c["step"] = "start"

    def finish_calibration(self):
        c, self.cal = self.cal, None
        self.last_cmd = ((0, 0), time.time())
        rx = c["res"].get("x")
        ry = c["res"].get("y")
        if not rx or not ry or max(abs(rx[0]), abs(rx[1])) < 3 or max(abs(ry[0]), abs(ry[1])) < 3:
            self.set_note("Yön ölçümü güvenilir değil (görüntü yeterince kaymadı); ölçülmüş varsayılan yönler kullanılıyor.")
            self.maps[c["key"]] = default_map(c["key"][1], bool(self.phone and self.phone.status.get("front")))
            return
        sign = lambda v: 1 if v >= 0 else -1
        # Hangi komut ekseni goruntude yatay hareket ettiriyor?
        if abs(rx[0]) >= abs(ry[0]):
            h, v = ("x", sign(rx[0])), ("y", sign(ry[1]))
        else:
            h, v = ("y", sign(ry[0])), ("x", sign(rx[1]))
        self.maps[c["key"]] = AxisMap(h, v)
        names = {"x": "yatay motor", "y": "dikey motor"}
        self.set_note(
            f"Yönler ölçüldü: sağ = {names[h[0]]} {'+' if h[1] > 0 else '−'}, yukarı = {names[v[0]]} {'+' if v[1] > 0 else '−'}"
        )
        print(f"Kalibrasyon {c['key']}: x->{rx} y->{ry} => h={h} v={v}")

    def toggle_tracking(self, on):
        self.tracking = on
        self.tracker.box = None
        self.last_wants_sent = 0.0  # telefondaki takibe hemen bildir
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
                if self.rb_wifi.isChecked() and host != self.ip.text().strip():
                    self.ip.setText(host)
                    self.settings.setValue("ip", host)
                    self.set_note(f"Telefonun yeni adresi bulundu: {host}")
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
        if st.get("gmode") and not self.cb_mode.view().isVisible():
            self.cb_mode.setCurrentIndex(int(st["gmode"]) - 1)
        if st.get("camera") is not None:
            i = self.cb_lens.findData(st.get("camera"))
            if i >= 0 and i != self.cb_lens.currentIndex():
                self.cb_lens.setCurrentIndex(i)

        ph.jitter = 0.2 if self.chk_smooth.isChecked() else 0.0
        ph.pump(time.time())
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
            if self.chk_horizon.isChecked():
                # Telefonun egikligi kadar ters cevir; kose bosluklari gorunmesin diye hafif yakinlas
                # Yercekimi sensoru zaten yumusak: hafif yumusatma, 0.5 derece olu bolge
                self.tilt_s = 0.6 * self.tilt_s + 0.4 * (sign * ph.tilt)
                a = max(-44.0, min(44.0, self.tilt_s))
                if abs(a) > 0.5:
                    # Zoom yok: goruntu ayni boyutta sadece doner; acilan koseler bulanik arka planla dolar
                    h, w = f0.shape[:2]
                    bg = cv2.resize(cv2.blur(cv2.resize(f0, (max(1, w // 10), max(1, h // 10))), (5, 5)), (w, h))
                    bg = (bg * 0.55).astype(f0.dtype)
                    M = cv2.getRotationMatrix2D((w / 2, h / 2), -a, 1.0)
                    f0 = cv2.warpAffine(f0, M, (w, h), dst=bg, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_TRANSPARENT)
            else:
                self.tilt_s = 0.0
            self.frame = f0

        # --- gimbal komutu ---
        now = time.time()
        if ph is not self._audio_phone:
            if self._audio_phone and self._on_audio in self._audio_phone.audio_listeners:
                self._audio_phone.audio_listeners.remove(self._on_audio)
            ph.audio_listeners.append(self._on_audio)
            self._audio_phone = ph
        self.room_tick(now)
        # Kullanilmayan ozellikler bant genisligi yemesin: telefona yalnizca gerekeni iste (2 sn'de bir tazele)
        if now - self.last_wants_sent > 2.0:
            self.last_wants_sent = now
            want_audio = self.btn_sound.isChecked() or bool(self.room and self.room.audio_wanted)
            self.send(t="audio", v=want_audio)
            self.send(t="tilt", v=self.chk_horizon.isChecked())
            # HEVC yalnizca oda kapaliyken (odadaki tarayicilar H.264 istiyor)
            self.send_track_params(st)
            want_hevc = self.chk_hevc.isChecked() and self.room is None
            self.send(t="codec", v="hevc" if want_hevc else "avc")
            if self.room:
                self.room.hevc_active = st.get("codec") == "hevc"
        key = (st.get("camera"), self.rot)
        amap = self.maps.get(key) or default_map(self.rot, bool(st.get("front")))
        if self.cal is not None:
            self.step_calibration(now, new)
            return_after = True
        else:
            return_after = False
            jx, jy = self.joy.value()
            kx = (1 if "R" in self.keys else 0) - (1 if "L" in self.keys else 0)
            ky = (1 if "U" in self.keys else 0) - (1 if "D" in self.keys else 0)
            manual = bool(jx or jy or kx or ky)
            vmove = self.room.current_move() if self.room else None
            if manual:
                mx, my = amap.to_cmd((jx + kx) * self.speed, (jy + ky) * self.speed)
            elif vmove is not None:
                # Odadaki izleyici joystick'i: takip 2 sn duraklar
                self.viewer_active_until = now + 2.0
                mx, my = amap.to_cmd(vmove[0] * self.speed, vmove[1] * self.speed)
            elif now < self.viewer_active_until:
                mx = my = 0
            elif self.tracking and key not in self.maps and self.frame is not None and ph.video_ok:
                self.start_calibration()
                mx = my = 0
            elif self.tracking and self.phone_tracks(st):
                # Takip telefonda (donanim yuz algilama, ag gecikmesi yok): PC hareket gondermez
                mx = my = 0
            elif self.tracking and ph.latest is not None and ph.latest_id != getattr(self, "_track_seen", -1):
                # Takip en taze kareyle calisir (ekrandaki akicilik tamponu ~0.2 sn geride; gecikme salinim yapar)
                self._track_seen = ph.latest_id
                self.tracker.dead = self.dead
                tf = ph.latest
                if self.rot == 90:
                    tf = cv2.rotate(tf, cv2.ROTATE_90_CLOCKWISE)
                elif self.rot == 180:
                    tf = cv2.rotate(tf, cv2.ROTATE_180)
                elif self.rot == 270:
                    tf = cv2.rotate(tf, cv2.ROTATE_90_COUNTERCLOCKWISE)
                right, up = self.tracker.update(tf, self.speed)
                mx, my = amap.to_cmd(right, up)
            elif self.tracking:
                mx, my = self.last_cmd[0] or (0, 0)
            else:
                mx = my = 0
            # --- sarj korumasi (uyku) ---
            charging = bool(st.get("gCharging"))
            wants = manual or vmove is not None or (self.tracking and (mx or my))
            if wants:
                self.last_activity = now
            if charging and self.chk_sleep.isChecked():
                if wants and now - self.wake_wanted_at > 30:
                    who = self.room.mover if (vmove is not None and self.room) else "Sen"
                    self.notify("Gimbal şarjda", f"{who} gimbal'ı kullanmak istiyor. Pili korumak için şarj kablosunu çıkar.")
                    self.wake_wanted_at = now
                mx = my = 0
            self.charge_tick(now, charging, st)
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
        if self.tracking and self.phone_tracks(st):
            fb = ph.face_box if time.time() - ph.face_at < 0.5 else None
            if fb:
                disp = f.copy()
                fh2, fw2 = f.shape[:2]
                cv2.rectangle(disp, (int(fb[0] * fw2), int(fb[1] * fh2)), (int(fb[2] * fw2), int(fb[3] * fh2)), (90, 220, 120), 3)
        elif self.tracking and self.tracker.box is not None:
            disp = f.copy()
            x, y, bw, bh = self.tracker.box
            cv2.rectangle(disp, (x, y), (x + bw, y + bh), (90, 220, 120), 3)
        self.view.show_frame(disp)
        gs = st.get("gimbal", "?")
        note = self.note[0] if time.time() - self.note[1] < 8 else ""
        self.statusbar.setText(
            (note + "   ·   " if note else "")
            + f"{fw}×{fh}  ·  {ph.fps:.0f} fps  ·  Gimbal: {gs} (kopma {st.get('drops', 0)})  ·  Takip: {('aranıyor…' if (ph.face_searching if self.phone_tracks(st) else self.tracker.searching) else ('açık · telefonda' if self.phone_tracks(st) else 'açık')) if self.tracking else 'kapalı'}"
        )

    def tracker_dead(self, dead):
        self.tracker.dead = dead

    def closeEvent(self, e):
        self.player.stop()
        self.talk_player.stop()
        if self.room:
            self.toggle_room(False)
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
