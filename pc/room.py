"""Oda: GimbalCam'i acan bilgisayarda calisan kucuk web sunucusu + Cloudflare gecici tuneli.

Merkezi sunucu yok. "Oda kur" denince:
  1) Bu bilgisayarda aiohttp sunucusu acilir (yalnizca 127.0.0.1).
  2) cloudflared "quick tunnel" ile https://<kod>.trycloudflare.com adresi alinir (hesap/DNS gerekmez).
  3) Izleyici kaanai.site/gimbal/oda sayfasindan oda kodu + sifreyle WebSocket'e baglanir.

WebSocket protokolu (wss://<kod>.trycloudflare.com/ws):
  izleyici -> {"t":"auth","password":"...","name":"..."}      ilk mesaj, 10 sn icinde
  oda      -> {"t":"ok"} | {"t":"err","reason":"..."}
  izleyici, auth icinde "codec":"h264" derse (WebCodecs) telefonun H.264 akisi aynen gelir; yoksa JPEG.
  oda      -> ikili mesaj: [tip 1 bayt][pts 8 bayt][veri]  tip: 1=H.264 anahtar (SPS/PPS dahil), 0=H.264, 2=JPEG, 3=ses PCM16 24 kHz mono
  izleyici -> {"t":"move","x":-1..1,"y":-1..1}                 x>0 = gorus saga, y>0 = gorus yukari
  izleyici -> {"t":"cmd","c":"track|center|zoom|ev|torch|autofocus|focus|camera|res|speed|recal", ...}
  oda      -> {"t":"info","viewers":n,"tracking":bool,"rot":0..270,...}   ara sira
"""

import asyncio
import hmac
import os
import re
import subprocess
import sys
import threading
import time

from aiohttp import WSMsgType, web

MAX_FAILS = 5          # IP basina hatali sifre hakki
LOCK_SECONDS = 300     # sonra bu kadar kilit
MAX_VIEWERS = 6


def _resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


class RoomServer:
    def __init__(self, password):
        self.password = password
        self.port = None
        self.viewers = {}           # ws -> {"name","ip","busy"}
        self.fails = {}             # ip -> (sayi, kilit_bitis)
        self.move = (0.0, 0.0, 0.0)  # (x, y, zaman)
        self.commands = []          # izleyicilerden gelen ozellik komutlari (Qt tarafi isler)
        self._h264_config = None
        self.key_wanted = False     # bir izleyici anahtar kare bekliyor: PC telefondan hemen istesin
        # Bas-konus: izleyicinin mikrofonu -> telefon hoparloru. Ayni anda tek konusan.
        self.on_talk = None         # fn(pcm_bytes, isim) - PC telefona iletir
        self.talker = None
        self.talker_at = 0.0
        self.mover = ""
        self.info = {}
        self._loop = None
        self._runner = None
        self._thread = None
        self._ready = threading.Event()

    # ---------- Qt tarafindan cagrilanlar (thread-safe) ----------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(10)
        return self.port

    def stop(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    @property
    def viewer_names(self):
        return [v["name"] for v in list(self.viewers.values())]

    hevc_active = False  # PC ayarlar: telefon su an HEVC gonderiyorsa tum izleyicilere JPEG

    @property
    def wants_jpeg(self):
        return any(v["mode"] == "jpeg" or self.hevc_active for v in list(self.viewers.values()))

    def push_frame(self, jpeg_bytes):
        """JPEG izleyicilere son kareyi gonder; yetismeyen izleyicide kare atlanir."""
        if self._loop and self.viewers:
            self._loop.call_soon_threadsafe(self._fanout, jpeg_bytes)

    def push_packet(self, data, pts, flags):
        """Telefonun H.264 paketi (Phone dinleyicisi; video is parcaciginda cagrilir)."""
        if flags & 8:
            # HEVC: tarayicilar cozemez (oda acilinca PC telefonu H.264'e alir; o arada JPEG gider)
            return
        if flags & 2:
            self._h264_config = data
            return
        if self._loop and self.viewers:
            self._loop.call_soon_threadsafe(self._fanout_h264, data, pts, bool(flags & 1))

    def _relay(self, src, kind, payload, audio):
        """Bir katilimcinin goruntu/sesini digerlerine ilet: [tur][gonderen id][veri].
        Goruntude alici mesgulse kare atlanir (gecikme birikmesin); seste en fazla birkac parca bekler."""
        sid = self.viewers.get(src, {}).get("id", 0)
        msg = bytes([kind, sid]) + payload
        for ws, v in list(self.viewers.items()):
            if ws is src or ws.closed:
                continue
            if audio:
                if v["relayAudio"] > 6:
                    continue
                v["relayAudio"] += 1
            else:
                if kind in v["relayBusy"]:
                    continue
                v["relayBusy"].add(kind)
            self._loop.create_task(self._relay_send(ws, v, msg, kind, audio))

    def _relay_h264(self, src, kind, key, payload):
        """H.264 aktarimi: alici mesgulse kare atlanamaz -> o alici bir sonraki anahtar kareye kadar bekler."""
        sid = self.viewers.get(src, {}).get("id", 0)
        msg = bytes([kind, sid, 1 if key else 0]) + payload
        for ws, v in list(self.viewers.items()):
            if ws is src or ws.closed:
                continue
            nk = v.setdefault("relayNeedKey", set())
            if (kind, sid) in nk:
                if not key:
                    continue
                nk.discard((kind, sid))
            if kind in v["relayBusy"]:
                nk.add((kind, sid))
                continue
            v["relayBusy"].add(kind)
            self._loop.create_task(self._relay_send(ws, v, msg, kind, False))

    async def _relay_send(self, ws, v, msg, kind, audio):
        try:
            await ws.send_bytes(msg)
        except Exception:
            pass
        finally:
            if audio:
                v["relayAudio"] -= 1
            else:
                v["relayBusy"].discard(kind)

    def push_audio(self, pcm):
        """Telefon sesi (PCM 16 bit, 24 kHz, mono) -> tum izleyicilere."""
        if self._loop and self.viewers:
            self._loop.call_soon_threadsafe(self._fanout_audio, pcm)

    def _fanout_audio(self, pcm):
        msg = b"\x03" + bytes(8) + pcm
        for ws, v in list(self.viewers.items()):
            # Goruntuyle ayni sirayla gider; ag tikaliysa biriktirme, parcayi at
            if ws.closed or not v.get("sound") or v.get("audioPending", 0) > 4:
                continue
            v["audioPending"] = v.get("audioPending", 0) + 1
            self._loop.create_task(self._send_audio(ws, v, msg))

    async def _send_audio(self, ws, v, msg):
        try:
            await ws.send_bytes(msg)
        except Exception:
            pass
        finally:
            v["audioPending"] -= 1

    def push_tilt(self, angle):
        """Ufuk kilidi acikken tarayicinin goruntuyu cevirmesi icin egiklik (sik gonderilir)."""
        if self._loop and self.viewers:
            self._loop.call_soon_threadsafe(self._fanout_json, {"t": "tilt", "a": round(angle, 1)})

    def _fanout_json(self, msg):
        for ws in list(self.viewers):
            if not ws.closed:
                self._loop.create_task(self._safe_json(ws, msg))

    async def _safe_json(self, ws, msg):
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    @property
    def audio_wanted(self):
        return any(v.get("sound") for v in list(self.viewers.values()))

    @property
    def talking(self):
        return self.talker if self.talker and time.time() - self.talker_at < 0.6 else None

    def pop_commands(self):
        cmds, self.commands = self.commands, []
        return cmds

    def set_info(self, **info):
        self.info.update(info)

    def current_move(self):
        """Son 0.4 sn icinde gelen izleyici komutu (yoksa None)."""
        x, y, t = self.move
        if time.time() - t > 0.4:
            return None
        return x, y

    # ---------- sunucu ----------
    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._startup())
        self._ready.set()
        self._loop.run_forever()

    async def _startup(self):
        app = web.Application()
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/", self._index)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self._loop.create_task(self._info_loop())

    async def _shutdown(self):
        for ws in list(self.viewers):
            await ws.close(message=b"oda kapandi")
        await self._runner.cleanup()
        self._loop.stop()

    async def _index(self, request):
        raise web.HTTPFound("https://kaanai.site/gimbal/oda/")

    def _client_ip(self, request):
        return request.headers.get("CF-Connecting-IP") or request.remote or "?"

    async def _ws(self, request):
        # Gorusme: katilimcilar ekran/kamera JPEG'i gonderebilir (en fazla ~512 KB kare)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=512 * 1024)
        await ws.prepare(request)
        ip = self._client_ip(request)
        n, until = self.fails.get(ip, (0, 0))
        if until > time.time():
            await ws.send_json({"t": "err", "reason": "Çok fazla hatalı deneme. Biraz sonra tekrar dene."})
            await ws.close()
            return ws
        try:
            msg = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            await ws.close()
            return ws
        try:
            data = msg.json() if msg.type == WSMsgType.TEXT else {}
        except ValueError:
            data = {}
        given = str(data.get("password", ""))
        if data.get("t") != "auth" or not hmac.compare_digest(given.encode(), self.password.encode()):
            n += 1
            self.fails[ip] = (n, time.time() + LOCK_SECONDS if n >= MAX_FAILS else 0)
            await asyncio.sleep(1.0)  # kaba kuvveti yavaslat
            await ws.send_json({"t": "err", "reason": "Şifre yanlış."})
            await ws.close()
            return ws
        if len(self.viewers) >= MAX_VIEWERS:
            await ws.send_json({"t": "err", "reason": "Oda dolu."})
            await ws.close()
            return ws
        self.fails.pop(ip, None)
        name = re.sub(r"[^\w çğıöşüÇĞİÖŞÜ.-]", "", str(data.get("name", "")))[:24] or "Misafir"
        mode = "h264" if data.get("codec") == "h264" else "jpeg"
        self._next_id = getattr(self, "_next_id", 0) % 250 + 1
        vid = self._next_id
        self.viewers[ws] = {"name": name, "ip": ip, "busy": False, "mode": mode, "needKey": True,
                            "id": vid, "relayBusy": set(), "relayAudio": 0, "sharing": set()}
        await ws.send_json({"t": "ok", "you": vid, **self.info})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    # [5][PCM16 24 kHz mono] bas-konus sesi
                    d = msg.data
                    if d[:1] == b"\x05" and 2 < len(d) <= 8000 and self.on_talk:
                        now = time.time()
                        if self.talker in (None, name) or now - self.talker_at > 1.0:
                            self.talker, self.talker_at = name, now
                            try:
                                self.on_talk(d[1:], name)
                            except Exception:
                                pass
                    elif d[:1] in (b"\x06", b"\x07") and 2 < len(d) <= 512 * 1024:
                        # Gorusme goruntusu: 6 = ekran/VR paylasimi, 7 = kamera (JPEG). Digerlerine aktar.
                        self.viewers[ws]["sharing"].add(d[0])
                        self._relay(ws, d[0], d[1:], audio=False)
                    elif d[:1] == b"\x0b" and 3 < len(d) <= 512 * 1024:
                        # Gorusme: VR/ekran paylasimi H.264 (tarayicida WebCodecs). [11][anahtar 0/1][Annex B]
                        self.viewers[ws]["sharing"].add(6)
                        self._relay_h264(ws, 11, d[1] == 1, d[2:])
                    elif d[:1] == b"\x09" and 2 < len(d) <= 8000:
                        # Gorusme sesi (surekli mikrofon, PCM16 24 kHz): digerlerine aktar, PC'de calinmaz
                        self._relay(ws, 9, d[1:], audio=True)
                    continue
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    d = msg.json()
                except ValueError:
                    continue
                if d.get("t") == "move":
                    try:
                        x = max(-1.0, min(1.0, float(d.get("x", 0))))
                        y = max(-1.0, min(1.0, float(d.get("y", 0))))
                    except (TypeError, ValueError):
                        continue
                    self.move = (x, y, time.time())
                    self.mover = name
                elif d.get("t") == "cmd" and d.get("c") == "sound":
                    # Izleyicinin ses tercihi: kapaliysa ona ses gonderilmez (bant genisligi)
                    self.viewers[ws]["sound"] = bool(d.get("v"))
                elif d.get("t") == "cmd" and isinstance(d.get("c"), str) and len(self.commands) < 50:
                    d["_from"] = name
                    self.commands.append(d)
        finally:
            self.viewers.pop(ws, None)
        return ws

    def _fanout(self, jpeg):
        msg = b"\x02" + bytes(8) + jpeg
        for ws, v in list(self.viewers.items()):
            if (v["mode"] != "jpeg" and not self.hevc_active) or v["busy"] or ws.closed:
                continue
            v["busy"] = True
            self._loop.create_task(self._send(ws, v, msg))

    def _fanout_h264(self, data, pts, key):
        for ws, v in list(self.viewers.items()):
            if v["mode"] != "h264" or ws.closed:
                continue
            if v["busy"]:
                # Ag yetismiyor: H.264'te kare atlanamaz, anahtar kareye kadar bekle (PC hemen ister)
                v["needKey"] = True
                self.key_wanted = True
                continue
            if v["needKey"]:
                if not key or not self._h264_config:
                    self.key_wanted = True
                    continue
                v["needKey"] = False
            payload = (self._h264_config + data) if key and self._h264_config else data
            msg = (b"\x01" if key else b"\x00") + max(pts, 0).to_bytes(8, "big") + payload
            v["busy"] = True
            self._loop.create_task(self._send(ws, v, msg))

    async def _send(self, ws, v, jpeg):
        try:
            await ws.send_bytes(jpeg)
        except Exception:
            pass
        finally:
            v["busy"] = False

    async def _info_loop(self):
        while True:
            await asyncio.sleep(1.5)
            # Gorusme: kim var, kim ekran/kamera paylasiyor (dosemelere isim yazmak icin)
            people = [{"id": v["id"], "name": v["name"], "sharing": sorted(v.get("sharing", ()))}
                      for v in list(self.viewers.values())]
            msg = {"t": "info", "viewers": len(self.viewers), "people": people, **self.info}
            for ws in list(self.viewers):
                try:
                    await ws.send_json(msg)
                except Exception:
                    pass


class QuickTunnel:
    """cloudflared ile hesapsiz gecici tunel: https://<kod>.trycloudflare.com"""

    def __init__(self, port):
        self.port = port
        self.url = None
        self.error = None
        self.proc = None

    def start(self):
        exe = _resource("bin", "cloudflared.exe")
        if not os.path.exists(exe):
            self.error = "cloudflared bulunamadı"
            return
        # Kullanicinin ~/.cloudflared/config.yml dosyasi (varsa adli tunel + ingress kurallari) karismasin:
        # bos bir ayar dosyasi ver. Yoksa istekler o kurallara takilip 404/500 doner.
        cfg_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "GimbalCam")
        os.makedirs(cfg_dir, exist_ok=True)
        cfg = os.path.join(cfg_dir, "tunnel.yml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("# GimbalCam oda tuneli icin bos ayar\n")
        self.proc = subprocess.Popen(
            [exe, "tunnel", "--config", cfg, "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        pending = None
        for line in self.proc.stdout:
            m = re.search(r"https://([a-z0-9-]+)\.trycloudflare\.com", line)
            if m and not pending:
                pending = m.group(0)
            # Adres tunel baglantisi kaydolunca kullanilabilir olur (oncesinde 404/500 doner)
            if pending and not self.url and "Registered tunnel connection" in line:
                self.url = pending
            if "failed" in line.lower() and "tunnel" in line.lower() and not self.url:
                self.error = line.strip()[-160:]

    @property
    def code(self):
        return self.url.split("//")[1].split(".")[0] if self.url else None

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class RoomDirectory:
    """ev.kaanai.site oda rehberi: oda listede gorunsun, kisa kodla (4 harf) katilinsin.
    Rehbere ulasilamazsa oda yine calisir; davet linki uzun tunel koduyla olur."""

    BASE = "https://ev.kaanai.site/gimbal/rooms"

    def __init__(self, name, url, viewers_fn):
        self.name, self.url, self.viewers_fn = name, url, viewers_fn
        self.id = None
        self.token = None
        self.error = None
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _post(self, path, payload, timeout=60):
        import json as _json
        import urllib.request
        req = urllib.request.Request(self.BASE + path, data=_json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())

    def _register(self):
        # Render uyuyorsa ilk istek 30-60 sn surebilir (soguk baslangic)
        d = self._post("", {"name": self.name, "url": self.url}, timeout=90)
        self.id, self.token, self.error = d["id"], d["token"], None

    def _run(self):
        while not self._stop.is_set():
            try:
                if not self.id:
                    self._register()
                else:
                    try:
                        self._post(f"/{self.id}/beat", {"token": self.token, "viewers": self.viewers_fn()})
                    except Exception as e:
                        if getattr(e, "code", None) == 404:   # rehber yeniden basladi: tekrar kaydol
                            self.id = None
                            continue
                        raise
            except Exception as e:
                self.error = str(e)[:120]
            self._stop.wait(20)

    def close(self):
        self._stop.set()
        if self.id:
            try:
                self._post(f"/{self.id}/close", {"token": self.token}, timeout=5)
            except Exception:
                pass
