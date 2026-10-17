"""Telefonu kablosuz hata ayiklama ile QR kodla eslestirir ve baglar.

Telefonda: Gelistirici secenekleri -> Kablosuz hata ayiklama -> Cihazi QR koduyla eslestir
ve pencerede cikan QR'i okut. Uygulamadan "Kablosuz eşleştir (QR)" butonuyla da acilir.
"""

import random
import re
import string
import subprocess
import sys
import threading
import time

import qrcode
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from gimbalcam import find_adb


def _rand(n):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def _adb(adb, *args, timeout=15):
    try:
        return subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _worker(adb, name, password, state):
    # 1) Telefon QR'i okutunca _adb-tls-pairing servisini yayinlar
    deadline = time.time() + 180
    while time.time() < deadline and not state["closed"]:
        out = _adb(adb, "mdns", "services")
        m = re.search(rf"{re.escape(name)}\s+_adb-tls-pairing\._tcp\.?\s+([\d.]+):(\d+)", out)
        if m:
            ip, port = m.group(1), m.group(2)
            state["msg"] = f"Eşleştiriliyor: {ip}:{port}"
            res = _adb(adb, "pair", f"{ip}:{port}", password, timeout=30)
            if "Successfully paired" not in res:
                state["msg"] = f"Eşleştirme başarısız:\n{res.strip()}"
                state["done"] = True
                return
            state["msg"] = "Eşleşti, bağlanılıyor…"
            # 2) Baglanti servisi (_adb-tls-connect) gorunene kadar bekle ve baglan
            for _ in range(30):
                out = _adb(adb, "mdns", "services")
                m2 = re.search(rf"\S+\s+_adb-tls-connect\._tcp\.?\s+({re.escape(ip)}):(\d+)", out)
                if m2:
                    res = _adb(adb, "connect", f"{m2.group(1)}:{m2.group(2)}")
                    state["ok"] = "connected" in res
                    state["msg"] = (f"Bağlandı: {m2.group(1)}\nBu pencereyi kapatıp GimbalCam'de “Bağlan”a bas."
                                    if state["ok"] else f"Bağlanamadı: {res.strip()}")
                    state["done"] = True
                    return
                time.sleep(1)
            state["msg"] = "Eşleşti ama bağlantı bulunamadı. Telefonda kablosuz hata ayıklama açık mı?"
            state["done"] = True
            return
        time.sleep(1)
    if not state["done"]:
        state["msg"] = "Süre doldu, QR okutulmadı."
        state["done"] = True


class PairWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kablosuz eşleştir")
        self.setStyleSheet("background:#0f1115; color:#e6e8ee; font-family:'Segoe UI'; font-size:11pt;")
        lay = QVBoxLayout(self)
        adb = find_adb()
        name = "gimbalcam-" + _rand(6)
        password = _rand(10)
        title = QLabel("Telefonda: Geliştirici seçenekleri → Kablosuz hata ayıklama →\n"
                       "“Cihazı QR koduyla eşleştir” deyip bu kodu okut.\n"
                       "(Telefon ve bilgisayar aynı Wi-Fi'de olmalı.)")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        img = qrcode.make(f"WIFI:T:ADB;S:{name};P:{password};;", box_size=10, border=3).convert("RGB")
        qimg = QImage(img.tobytes(), img.width, img.height, 3 * img.width, QImage.Format_RGB888).copy()
        qr = QLabel()
        qr.setPixmap(QPixmap.fromImage(qimg))
        qr.setAlignment(Qt.AlignCenter)
        lay.addWidget(qr)
        self.status = QLabel("QR okutulması bekleniyor…")
        self.status.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.status)
        self.state = {"msg": self.status.text(), "done": False, "ok": False, "closed": False}
        if not adb:
            self.state.update(msg="adb bulunamadı.", done=True)
        else:
            threading.Thread(target=_worker, args=(adb, name, password, self.state), daemon=True).start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(300)

    def _poll(self):
        self.status.setText(self.state["msg"])
        if self.state["done"]:
            self.status.setStyleSheet("color:#30c46b;" if self.state["ok"] else "color:#f5a524;")

    def closeEvent(self, e):
        self.state["closed"] = True
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    w = PairWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
