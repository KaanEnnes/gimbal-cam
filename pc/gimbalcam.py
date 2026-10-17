"""GimbalCam PC programi.

Telefondaki GimbalCam uygulamasindan goruntuyu alir, pencerede gosterir,
gimbal'i klavye/fare ile kontrol eder, istenirse yuzu otomatik takip eder
ve goruntuyu sanal kamera olarak OBS/Discord/Zoom'a verir.

Calistirma:
    python gimbalcam.py              # USB (adb) ile
    python gimbalcam.py --host 192.168.1.6   # Wi-Fi ile

Tuslar:
    Ok tuslari / WASD  gimbal'i cevir      + / -   gimbal hizi
    T  yuz takibi ac/kapa                  C       gimbal'i ortala
    Z / X  yakinlas / uzaklas              F       otomatik odak
    Fare sol tik  oraya odaklan            K       on/arka kamera
    E / R  pozlama -/+                     L       fener
    1 / 2 / 3  720p / 1080p / 720p60       V       sanal kamera ac/kapa
    H  yardimi gizle/goster                Q / Esc cikis
"""

import argparse
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

import av
import cv2
import numpy as np

VIDEO_PORT = 8686
CONTROL_PORT = 8687

KEY_LEFT, KEY_UP, KEY_RIGHT, KEY_DOWN = 2424832, 2490368, 2555904, 2621440


def find_adb():
    adb = shutil.which("adb")
    if adb:
        return adb
    local = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe")
    return local if os.path.exists(local) else None


def run(cmd, timeout=6):
    """adb cagrisi; adb takilirsa (kablosuz baglanti offline) uygulama donmasin."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(cmd, 1, "", "")


def usb_devices(adb):
    """adb'ye bagli cihazlar (USB ve kablosuz hata ayiklama). GimbalCam kurulu olanlar once gelir."""
    out = run([adb, "devices"]).stdout
    devs = [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]

    def has_app(d):
        r = run([adb, "-s", d, "shell", "pm", "path", "com.mrkaan840.gimbalcam"])
        return "package:" in r.stdout

    with_app = [d for d in devs if has_app(d)]
    return with_app or devs


def setup_usb(serial=None):
    adb = find_adb()
    if not adb:
        return False
    devs = usb_devices(adb)
    if not devs:
        return False
    # Ayni telefon hem USB hem kablosuz bagliysa USB'yi tercih et
    serial = serial or sorted(devs, key=lambda d: ("." in d or ":" in d))[0]
    # Uygulama kapaliysa ac
    run([adb, "-s", serial, "shell", "am", "start", "-n", "com.mrkaan840.gimbalcam/.MainActivity"])
    m = re.match(r"^(\d+\.\d+\.\d+\.\d+):\d+$", serial)
    if not m and "_adb-tls-connect" in serial:
        out = run([adb, "-s", serial, "shell", "ip", "-4", "addr", "show", "wlan0"]).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        # Kablosuz adb: goruntuyu adb tuneli yerine dogrudan Wi-Fi'den al (daha az gecikme)
        print(f"Kablosuz baglanti: {m.group(1)}")
        return m.group(1)
    for port in (VIDEO_PORT, CONTROL_PORT):
        run([adb, "-s", serial, "forward", f"tcp:{port}", f"tcp:{port}"])
    print(f"USB baglantisi: {serial}")
    return "127.0.0.1"


class Phone:
    """Telefonla goruntu ve kontrol baglantisi."""

    def __init__(self, host):
        self.host = host
        self.frame = None
        self.frame_id = 0
        self.fps = 0.0
        self.status = {}
        self.video_ok = False
        self.control_ok = False
        self._ctl = None
        self._ctl_lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._video_loop, daemon=True).start()
        threading.Thread(target=self._control_loop, daemon=True).start()

    def close(self):
        self._stop = True

    # ---- goruntu ----
    def _recv_exact(self, s, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("baglanti kapandi")
            buf += chunk
        return bytes(buf)

    def _video_loop(self):
        while not self._stop:
            try:
                s = socket.create_connection((self.host, VIDEO_PORT), timeout=5)
                s.settimeout(10)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)  # buyuk tampon = gecikme
                codec = av.CodecContext.create("h264", "r")
                codec.options = {"flags": "low_delay", "flags2": "fast"}
                # Kare-iş parçacığı kod çözme her iş parçacığı için bir kare gecikme ekler
                codec.thread_type = "SLICE"
                self.video_ok = True
                count, t0 = 0, time.time()
                while not self._stop:
                    size, _pts, _flags = struct.unpack(">iqb", self._recv_exact(s, 13))
                    data = self._recv_exact(s, size)
                    for pkt in codec.parse(data):
                        for fr in codec.decode(pkt):
                            self.frame = fr.to_ndarray(format="bgr24")
                            self.frame_id += 1
                            count += 1
                    if time.time() - t0 >= 1:
                        self.fps = count / (time.time() - t0)
                        count, t0 = 0, time.time()
            except Exception as e:
                self.video_ok = False
                if not self._stop:
                    print(f"Goruntu baglantisi bekleniyor... ({e})")
                    time.sleep(1.5)

    # ---- kontrol ----
    def _control_loop(self):
        while not self._stop:
            try:
                s = socket.create_connection((self.host, CONTROL_PORT), timeout=5)
                s.settimeout(None)
                with self._ctl_lock:
                    self._ctl = s
                self.control_ok = True
                f = s.makefile("r", encoding="utf-8")
                for line in f:
                    try:
                        msg = json.loads(line)
                        if msg.get("t") == "status":
                            self.status = msg
                    except ValueError:
                        pass
                raise ConnectionError("kontrol kapandi")
            except Exception:
                self.control_ok = False
                with self._ctl_lock:
                    self._ctl = None
                if not self._stop:
                    time.sleep(1.5)

    def send(self, **msg):
        line = (json.dumps(msg) + "\n").encode()
        with self._ctl_lock:
            if self._ctl is None:
                return
            try:
                self._ctl.sendall(line)
            except OSError:
                self._ctl = None


class FaceTracker:
    """Yuzu bulur ve onu ortada tutacak gimbal hizini hesaplar."""

    def __init__(self):
        self.det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        self.box = None
        self.lost = 0
        self.sx = 0.0
        self.sy = 0.0
        self.dead = 0.08
        # Eksen yonleri (+1/-1). Kameraya/yone gore degisir; takip sirasinda kendini ayarlar.
        self.signs = {}
        self.key = None
        self._win = [None, None]  # eksen basina (baslangic zamani, baslangic hatasi)

    def _learn(self, axis, err):
        """Gimbal bir yone donerken yuz merkezden uzaklasiyorsa o eksenin yonunu cevir."""
        now = time.time()
        w = self._win[axis]
        if abs(err) < 0.2:
            self._win[axis] = None
            return
        if w is None or (w[1] > 0) != (err > 0):
            self._win[axis] = (now, err)
            return
        if now - w[0] < 0.8:
            return
        if abs(err) > abs(w[1]) + 0.06:
            sx, sy = self.signs.get(self.key, (1, 1))
            self.signs[self.key] = (-sx, sy) if axis == 0 else (sx, -sy)
            print(f"Takip yonu duzeltildi: {'yatay' if axis == 0 else 'dikey'} eksen ters ({self.key})")
        self._win[axis] = (now, err)

    def update(self, frame, max_speed, key=None):
        self.key = key
        h, w = frame.shape[:2]
        scale = 320 / w
        small = cv2.cvtColor(cv2.resize(frame, (320, int(h * scale))), cv2.COLOR_BGR2GRAY)
        small = cv2.equalizeHist(small)
        faces = self.det.detectMultiScale(small, scaleFactor=1.15, minNeighbors=5, minSize=(24, 24))
        if len(faces):
            faces = [tuple(int(v / scale) for v in f) for f in faces]
            if self.box is not None:
                px, py = self.box[0] + self.box[2] / 2, self.box[1] + self.box[3] / 2
                self.box = min(faces, key=lambda f: (f[0] + f[2] / 2 - px) ** 2 + (f[1] + f[3] / 2 - py) ** 2)
            else:
                self.box = max(faces, key=lambda f: f[2] * f[3])
            self.lost = 0
        else:
            self.lost += 1
            if self.lost > 8:
                self.box = None
        if self.box is None:
            self.sx *= 0.5
            self.sy *= 0.5
            return 0, 0
        x, y, bw, bh = self.box
        ex = ((x + bw / 2) - w / 2) / (w / 2)  # -1..1, + = sagda
        ey = (h / 2 - (y + bh / 2.5)) / (h / 2)  # + = yukarida (yuzu biraz ust tarafa koy)
        dead = self.dead
        self._learn(0, ex)
        self._learn(1, ey)
        sgx, sgy = self.signs.get(self.key, (1, 1))
        tx = 0 if abs(ex) < dead else ex * sgx
        ty = 0 if abs(ey) < dead else ey * sgy
        # Yumusatma: ani hiz sicramalari olmasin
        self.sx = self.sx * 0.6 + tx * 0.4
        self.sy = self.sy * 0.6 + ty * 0.4
        return int(self.sx * max_speed), int(self.sy * max_speed)


def main():
    ap = argparse.ArgumentParser(description="GimbalCam PC")
    ap.add_argument("--host", help="Telefonun Wi-Fi IP adresi (bos birakilirsa USB kullanilir)")
    ap.add_argument("--serial", help="Birden fazla telefon bagliysa adb seri numarasi")
    ap.add_argument("--vcam", action="store_true", help="Sanal kamerayi baslangicta ac")
    args = ap.parse_args()

    host = args.host
    if not host:
        host = setup_usb(args.serial)
        if not host:
            print("USB ile telefon bulunamadi. Wi-Fi icin: python gimbalcam.py --host <telefon IP>")
            return

    phone = Phone(host)
    tracker = FaceTracker()
    win = "GimbalCam"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1280, 720)

    speed = 600
    tracking = False
    zoom = 1.0
    ev = 0
    torch = False
    show_help = True
    held = {}  # yon -> son basilma zamani
    last_sent = (None, 0.0)
    vcam = None
    want_vcam = args.vcam
    shown_id = -1
    view_frame = None
    camera_ids = ["0", "1"]
    cam_idx = 0

    def on_mouse(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN and view_frame is not None:
            try:
                _, _, ww, wh = cv2.getWindowImageRect(win)
            except Exception:
                return
            phone.send(t="focus", x=x / max(ww, 1), y=y / max(wh, 1))

    cv2.setMouseCallback(win, on_mouse)

    while True:
        frame = phone.frame
        if frame is not None and phone.frame_id != shown_id:
            shown_id = phone.frame_id
            view_frame = frame

        now = time.time()
        # --- gimbal hareketi ---
        mx = my = 0
        manual = False
        for k, t in list(held.items()):
            if now - t > 0.18:
                del held[k]
        if "L" in held: mx -= 1; manual = True
        if "R" in held: mx += 1; manual = True
        if "U" in held: my += 1; manual = True
        if "D" in held: my -= 1; manual = True
        mx *= speed
        my *= speed
        if tracking and not manual and view_frame is not None:
            mx, my = tracker.update(view_frame, speed)
        cmd = (mx, my)
        if cmd != last_sent[0] or (cmd != (0, 0) and now - last_sent[1] > 0.1):
            phone.send(t="move", x=mx, y=my)
            last_sent = (cmd, now)

        # --- sanal kamera ---
        if want_vcam and vcam is None and view_frame is not None:
            try:
                import pyvirtualcam
                h, w = view_frame.shape[:2]
                vcam = pyvirtualcam.Camera(width=w, height=h, fps=30, fmt=pyvirtualcam.PixelFormat.BGR)
                print(f"Sanal kamera acik: {vcam.device}")
            except Exception as e:
                print(f"Sanal kamera acilamadi: {e}\n(OBS Studio kurulu olmali; OBS'te bir kez 'Sanal Kamerayi Baslat' deyin.)")
                want_vcam = False
        if vcam is not None and view_frame is not None:
            h, w = view_frame.shape[:2]
            if (w, h) != (vcam.width, vcam.height):
                vcam.close()
                vcam = None
            else:
                vcam.send(view_frame)
        if not want_vcam and vcam is not None:
            vcam.close()
            vcam = None

        # --- ekran ---
        if view_frame is not None:
            disp = view_frame.copy()
        else:
            disp = np.zeros((720, 1280, 3), np.uint8)
            cv2.putText(disp, "Telefon goruntusu bekleniyor...", (60, 360), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
        if tracking and tracker.box is not None:
            x, y, bw, bh = tracker.box
            cv2.rectangle(disp, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        st = phone.status
        lines = [
            f"Gimbal: {st.get('gimbal', '?')}   Takip: {'ACIK' if tracking else 'kapali'}   Hiz: {speed}",
            f"{st.get('w', '?')}x{st.get('h', '?')}  {phone.fps:.0f} fps   Zoom {zoom:.1f}x  EV {ev}   Sanal kamera: {'ACIK' if vcam else 'kapali'}",
        ]
        if show_help:
            lines += [
                "Oklar/WASD: cevir  +/-: hiz  T: takip  C: ortala  Z/X: zoom  F: oto odak  tik: odak",
                "K: kamera  E/R: pozlama  L: fener  1/2/3: 720p/1080p/720p60  V: sanal kamera  H: yardim  Q: cikis",
            ]
        for i, text in enumerate(lines):
            cv2.putText(disp, text, (12, 28 + i * 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4)
            cv2.putText(disp, text, (12, 28 + i * 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.imshow(win, disp)

        key = cv2.waitKeyEx(10)
        if cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
            break
        if key == -1:
            continue
        ch = chr(key & 0xFF).lower() if key < 256 else ""
        if key in (KEY_LEFT,) or ch == "a": held["L"] = now
        elif key in (KEY_RIGHT,) or ch == "d": held["R"] = now
        elif key in (KEY_UP,) or ch == "w": held["U"] = now
        elif key in (KEY_DOWN,) or ch == "s": held["D"] = now
        elif ch in ("+", "="): speed = min(3000, speed + 100)
        elif ch == "-": speed = max(100, speed - 100)
        elif ch == "t":
            tracking = not tracking
            tracker.box = None
        elif ch == "c": phone.send(t="center")
        elif ch == "z": zoom = min(10.0, zoom + 0.25); phone.send(t="zoom", v=zoom)
        elif ch == "x": zoom = max(1.0, zoom - 0.25); phone.send(t="zoom", v=zoom)
        elif ch == "f": phone.send(t="autofocus")
        elif ch == "e": ev -= 1; phone.send(t="ev", v=ev)
        elif ch == "r": ev += 1; phone.send(t="ev", v=ev)
        elif ch == "l": torch = not torch; phone.send(t="torch", v=torch)
        elif ch == "k":
            ids = [c.split(":")[0] for c in st.get("cameras", [])] or camera_ids
            cam_idx = (cam_idx + 1) % len(ids)
            phone.send(t="camera", id=ids[cam_idx])
        elif ch == "1": phone.send(t="res", w=1280, h=720, fps=30)
        elif ch == "2": phone.send(t="res", w=1920, h=1080, fps=30)
        elif ch == "3": phone.send(t="res", w=1280, h=720, fps=60)
        elif ch == "v": want_vcam = not want_vcam
        elif ch == "h": show_help = not show_help
        elif ch == "q" or key == 27: break

    phone.send(t="stop")
    time.sleep(0.1)
    phone.close()
    if vcam:
        vcam.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
