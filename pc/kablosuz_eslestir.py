"""Telefonu kablosuz hata ayiklama ile QR kodla eslestirir ve baglar.

Telefonda: Gelistirici secenekleri -> Kablosuz hata ayiklama -> Cihazi QR koduyla eslestir
ve bu pencerede cikan QR'i okut.
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

ADB = find_adb()


def rand(n):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def adb(*args, timeout=15):
    try:
        return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


def worker(name, password, state):
    # 1) Telefon QR'i okutunca _adb-tls-pairing servisini yayinlar
    deadline = time.time() + 180
    while time.time() < deadline and not state["done"]:
        out = adb("mdns", "services")
        m = re.search(rf"{re.escape(name)}\s+_adb-tls-pairing\._tcp\.?\s+([\d.]+):(\d+)", out)
        if m:
            ip, port = m.group(1), m.group(2)
            state["msg"] = f"Eşleştiriliyor: {ip}:{port}"
            res = adb("pair", f"{ip}:{port}", password, timeout=30)
            if "Successfully paired" not in res:
                state["msg"] = f"Eşleştirme başarısız:\n{res.strip()}"
                state["done"] = True
                return
            state["msg"] = "Eşleşti, bağlanılıyor…"
            # 2) Baglanti servisi (_adb-tls-connect) gorunene kadar bekle ve baglan
            for _ in range(30):
                out = adb("mdns", "services")
                m2 = re.search(rf"\S+\s+_adb-tls-connect\._tcp\.?\s+({re.escape(ip)}):(\d+)", out)
                if m2:
                    res = adb("connect", f"{m2.group(1)}:{m2.group(2)}")
                    state["msg"] = f"Bağlandı: {m2.group(1)}:{m2.group(2)}\nArtık kabloyu çıkarabilirsin." if "connected" in res else f"Bağlanamadı: {res.strip()}"
                    state["ok"] = "connected" in res
                    state["done"] = True
                    return
                time.sleep(1)
            state["msg"] = "Eşleşti ama bağlantı servisi bulunamadı. Telefonda kablosuz hata ayıklama açık mı?"
            state["done"] = True
            return
        time.sleep(1)
    if not state["done"]:
        state["msg"] = "Süre doldu, QR okutulmadı."
        state["done"] = True


def main():
    if not ADB:
        print("adb bulunamadi")
        sys.exit(1)
    name = "gimbalcam-" + rand(6)
    password = rand(10)
    data = f"WIFI:T:ADB;S:{name};P:{password};;"

    img = qrcode.make(data, box_size=10, border=3).convert("RGB")
    qimg = QImage(img.tobytes(), img.width, img.height, 3 * img.width, QImage.Format_RGB888)

    app = QApplication(sys.argv)
    w = QWidget()
    w.setWindowTitle("Kablosuz hata ayıklama")
    w.setStyleSheet("background:#0f1115; color:#e6e8ee; font-family:'Segoe UI'; font-size:11pt;")
    lay = QVBoxLayout(w)
    title = QLabel("Telefonda: Geliştirici seçenekleri → Kablosuz hata ayıklama →\n“Cihazı QR koduyla eşleştir” deyip bu kodu okut.\n(Telefon ve bilgisayar aynı Wi-Fi'de olmalı.)")
    title.setAlignment(Qt.AlignCenter)
    lay.addWidget(title)
    qr = QLabel()
    qr.setPixmap(QPixmap.fromImage(qimg.copy()))
    qr.setAlignment(Qt.AlignCenter)
    lay.addWidget(qr)
    status = QLabel("QR okutulması bekleniyor…")
    status.setAlignment(Qt.AlignCenter)
    lay.addWidget(status)

    state = {"msg": status.text(), "done": False, "ok": False}
    threading.Thread(target=worker, args=(name, password, state), daemon=True).start()

    def poll():
        status.setText(state["msg"])
        if state["done"]:
            status.setStyleSheet("color:#30c46b;" if state["ok"] else "color:#f5a524;")

    t = QTimer()
    t.timeout.connect(poll)
    t.start(300)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
