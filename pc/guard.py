"""Kapi bekcisi: kapi bolgesinde hareket ya da tanimadigi bir yuz gorurse secilen pencereyi kucultur.

- Kapi bolgesi: goruntude 0..1 dikdortgen (fareyle cizilir).
- "Beni tani": o anki en buyuk yuzun SFace parmak izi kaydedilir; sonra bu yuze benzemeyen her yuz yabanci sayilir.
- Pencere: Windows'ta acik pencerelerden biri; tetiklenince ShowWindow(SW_MINIMIZE).
"""
import ctypes
import json
import os
import time
from ctypes import wintypes

import cv2
import numpy as np

from gimbalcam import resource

user32 = ctypes.windll.user32
SW_MINIMIZE, SW_RESTORE = 6, 9


def list_windows():
    """Gorunur, basligi olan ust duzey pencereler: [(hwnd, baslik)]"""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value.strip()
                if t and t not in ("Program Manager", "Settings", "Ayarlar"):
                    out.append((hwnd, t))
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_window(title_part):
    if not title_part:
        return None
    for hwnd, t in list_windows():
        if title_part.lower() in t.lower():
            return hwnd
    return None


class Guard:
    MATCH = 0.36          # SFace kosinus benzerligi esigi (ustu = ayni kisi)

    def __init__(self, cfg_path):
        self.cfg_path = cfg_path
        self.enabled = False
        self.door = None          # (x0, y0, x1, y1) 0..1
        self.me = []              # kayitli yuz ozellikleri
        self.window = ""          # kucultulecek pencerenin basligindan bir parca
        self.load()
        self.det = None
        self.rec = None
        try:
            self.det = cv2.FaceDetectorYN.create(resource("face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.7, 0.3, 20)
            self.rec = cv2.FaceRecognizerSF.create(resource("face_recognition_sface_2021dec.onnx"), "")
        except Exception as e:
            print(f"Bekci: yuz modelleri acilamadi: {e}")
        self.prev = None
        self.motion_hits = 0
        self.last_run = 0.0
        self.fired_at = 0.0
        self.reason = ""
        self.faces = []           # son bakista: [(x0,y0,x1,y1 0..1, tanidik_mi)]

    # ---- ayarlar ----
    def load(self):
        try:
            d = json.load(open(self.cfg_path, encoding="utf-8"))
            self.door = tuple(d["door"]) if d.get("door") else None
            self.me = [np.array(f, np.float32).reshape(1, -1) for f in d.get("me", [])]
            self.window = d.get("window", "")
            self.enabled = bool(d.get("enabled", False))
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.cfg_path), exist_ok=True)
            json.dump({"door": self.door, "me": [f.flatten().tolist() for f in self.me],
                       "window": self.window, "enabled": self.enabled}, open(self.cfg_path, "w", encoding="utf-8"))
        except Exception as e:
            print(f"Bekci ayari kaydedilemedi: {e}")

    # ---- yuzler ----
    def _faces(self, frame):
        if self.det is None:
            return [], frame
        h, w = frame.shape[:2]
        sc = min(1.0, 640 / max(w, h))
        small = cv2.resize(frame, (int(w * sc), int(h * sc))) if sc < 1 else frame
        self.det.setInputSize((small.shape[1], small.shape[0]))
        _, f = self.det.detect(small)
        return ([] if f is None else list(f)), small

    def enroll(self, frame):
        """En buyuk yuzu 'ben' olarak ekle. Donus: basarili mi."""
        faces, small = self._faces(frame)
        if not faces or self.rec is None:
            return False
        f = max(faces, key=lambda r: r[2] * r[3])
        feat = self.rec.feature(self.rec.alignCrop(small, f))
        self.me.append(feat)
        self.me = self.me[-5:]     # farkli acilardan en fazla 5 kayit
        self.save()
        return True

    def _is_me(self, feat):
        return any(self.rec.match(feat, m, cv2.FaceRecognizerSF_FR_COSINE) >= self.MATCH for m in self.me)

    # ---- her karede ----
    def check(self, frame):
        """Tetiklenirse sebep metnini dondurur, yoksa None. Saniyede ~5 kez bakar."""
        now = time.time()
        if not self.enabled or now - self.last_run < 0.2:
            return None
        self.last_run = now
        reason = None
        # 1) Kapi bolgesinde hareket
        if self.door:
            h, w = frame.shape[:2]
            x0, y0, x1, y1 = self.door
            roi = frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
            if roi.size:
                g = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(roi, (160, 120)), cv2.COLOR_BGR2GRAY), (7, 7), 0)
                if self.prev is not None and self.prev.shape == g.shape:
                    changed = np.count_nonzero(cv2.absdiff(g, self.prev) > 28) / g.size
                    self.motion_hits = self.motion_hits + 1 if changed > 0.06 else 0
                    if self.motion_hits >= 2:          # iki ardisik bakis: tek kare gurultusunu yoksay
                        reason = "Kapıda hareket"
                self.prev = g
        # 2) Tanimadigim yuz
        faces, small = self._faces(frame)
        sh, sw = small.shape[:2]
        self.faces = []
        for f in faces:
            known = True
            if self.me and self.rec is not None:
                try:
                    known = self._is_me(self.rec.feature(self.rec.alignCrop(small, f)))
                except Exception:
                    pass
            elif len(faces) > 1:
                known = False          # kayit yoksa: ikinci yuz yabanci sayilir
            self.faces.append(((f[0] / sw, f[1] / sh, (f[0] + f[2]) / sw, (f[1] + f[3]) / sh), known))
            if not known and f[2] * f[3] > 0.004 * sw * sh:
                reason = reason or "Tanımadığım biri"
        if reason and now - self.fired_at > 3:
            self.fired_at = now
            self.reason = reason
            self.hide()
            return reason
        return None

    def hide(self):
        hwnd = find_window(self.window)
        if hwnd:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
        return bool(hwnd)

    def restore(self):
        hwnd = find_window(self.window)
        if hwnd:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        self.motion_hits = 0
        self.prev = None
