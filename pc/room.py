"""Oda: GimbalCam'i acan bilgisayarda calisan kucuk web sunucusu + Cloudflare gecici tuneli.

Merkezi sunucu yok. "Oda kur" denince:
  1) Bu bilgisayarda aiohttp sunucusu acilir (yalnizca 127.0.0.1).
  2) cloudflared "quick tunnel" ile https://<kod>.trycloudflare.com adresi alinir (hesap/DNS gerekmez).
  3) Izleyici kaanai.site/gimbal/oda sayfasindan oda kodu + sifreyle WebSocket'e baglanir.

WebSocket protokolu (wss://<kod>.trycloudflare.com/ws):
  izleyici -> {"t":"auth","password":"...","name":"..."}      ilk mesaj, 10 sn icinde
  oda      -> {"t":"ok"} | {"t":"err","reason":"..."}
  oda      -> ikili mesaj: JPEG kare
  izleyici -> {"t":"move","x":-1..1,"y":-1..1}                 x>0 = gorus saga, y>0 = gorus yukari
  oda      -> {"t":"info","viewers":n,"tracking":bool,...}   ara sira
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

    def push_frame(self, jpeg_bytes):
        """Son kareyi bekleyen her izleyiciye gonder; yetismeyen izleyicide kare atlanir."""
        if self._loop and self.viewers:
            self._loop.call_soon_threadsafe(self._fanout, jpeg_bytes)

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
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
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
        self.viewers[ws] = {"name": name, "ip": ip, "busy": False}
        await ws.send_json({"t": "ok", **self.info})
        try:
            async for msg in ws:
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
        finally:
            self.viewers.pop(ws, None)
        return ws

    def _fanout(self, jpeg):
        for ws, v in list(self.viewers.items()):
            if v["busy"] or ws.closed:
                continue
            v["busy"] = True
            self._loop.create_task(self._send(ws, v, jpeg))

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
            msg = {"t": "info", "viewers": len(self.viewers), **self.info}
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
