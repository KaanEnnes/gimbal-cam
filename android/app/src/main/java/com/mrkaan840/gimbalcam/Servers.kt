package com.mrkaan840.gimbalcam

import android.media.MediaCodec
import org.json.JSONObject
import java.io.BufferedReader
import java.io.DataOutputStream
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CopyOnWriteArrayList

const val VIDEO_PORT = 8686
const val CONTROL_PORT = 8687
/** Istemci basina en fazla bekleyen kare (~250 ms). Fazlasi gecikme demek. */
const val QUEUE_FRAMES = 8

/**
 * Goruntu sunucusu. Her kare su sekilde gider:
 *   uzunluk(int32) | pts mikrosaniye(int64) | bayraklar(int8) | H.264 Annex-B verisi
 * Bayrak 2 = codec ayari (SPS/PPS), 1 = anahtar kare.
 */
class VideoServer(
    private val log: (String) -> Unit,
    private val onClient: () -> Unit,
    private val onCongestion: () -> Unit,
) {
    private class Client(val sock: Socket) {
        val q = ArrayBlockingQueue<Triple<ByteArray, Long, Int>>(QUEUE_FRAMES)
        @Volatile var needKey = true
        @Volatile var lastSentAt = System.currentTimeMillis()
    }

    private val clients = CopyOnWriteArrayList<Client>()
    @Volatile private var config: ByteArray? = null
    private var server: ServerSocket? = null
    val clientCount get() = clients.size

    fun start() {
        Thread({
            try {
                val ss = ServerSocket(VIDEO_PORT)
                server = ss
                while (!ss.isClosed) {
                    val s = ss.accept()
                    s.tcpNoDelay = true
                    // Kucuk tampon: ag yavaslarsa kareler telefonda birikip gecikme yaratmasin
                    s.sendBufferSize = 64 * 1024
                    val c = Client(s)
                    clients.add(c)
                    log("PC goruntuye baglandi: ${s.inetAddress.hostAddress}")
                    Thread({ writer(c) }, "video-yaz").start()
                    onClient()
                }
            } catch (e: Exception) {
                log("Goruntu sunucusu durdu: ${e.message}")
            }
        }, "video-sunucu").start()
    }

    fun stop() {
        try { server?.close() } catch (_: Exception) {}
        clients.forEach { try { it.sock.close() } catch (_: Exception) {} }
    }

    fun push(data: ByteArray, pts: Long, flags: Int) {
        val isConfig = flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
        val isKey = flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0
        if (isConfig) config = data
        val f = (if (isConfig) 2 else 0) or (if (isKey) 1 else 0)
        for (c in clients) {
            if (isConfig) continue
            if (c.needKey) {
                if (!isKey) continue
                val cfg = config ?: continue
                c.q.clear()
                c.q.offer(Triple(cfg, pts, 2))
                c.needKey = false
            }
            if (!c.q.offer(Triple(data, pts, f))) {
                c.q.clear()
                c.needKey = true
                if (System.currentTimeMillis() - c.lastSentAt > 3000) {
                    // 3 sn'dir hic yazilamiyor: PC kapanmis, olu baglantiyi at
                    log("Yanit vermeyen PC baglantisi kapatildi")
                    try { c.sock.close() } catch (_: Exception) {}
                    clients.remove(c)
                    continue
                }
                // Ag yetismiyor: kuyrugu bosalt, bir sonraki anahtar kareden devam et
                onCongestion()
                onClient()
            }
        }
    }

    private fun writer(c: Client) {
        try {
            val out = DataOutputStream(c.sock.getOutputStream().buffered(1 shl 16))
            while (true) {
                val (d, pts, f) = c.q.take()
                out.writeInt(d.size)
                out.writeLong(pts)
                out.writeByte(f)
                out.write(d)
                if (c.q.isEmpty()) out.flush()
                c.lastSentAt = System.currentTimeMillis()
            }
        } catch (_: Exception) {
        } finally {
            clients.remove(c)
            try { c.sock.close() } catch (_: Exception) {}
            log("PC goruntu baglantisi kapandi")
        }
    }
}

/**
 * Kontrol sunucusu: satir satir JSON.
 * PC -> telefon: {"t":"move","x":..,"y":..} {"t":"stop"} {"t":"center"} {"t":"zoom","v":2.0}
 *                {"t":"focus","x":0.5,"y":0.5} {"t":"autofocus"} {"t":"ev","v":0} {"t":"torch","v":true}
 *                {"t":"camera","id":"1"} {"t":"res","w":1920,"h":1080,"fps":30} {"t":"bitrate","v":8000000}
 * Telefon -> PC: {"t":"status", ...}
 */
class ControlServer(private val log: (String) -> Unit, private val onCommand: (JSONObject) -> Unit) {
    private val outs = CopyOnWriteArrayList<OutputStreamWriter>()
    private var server: ServerSocket? = null

    fun start() {
        Thread({
            try {
                val ss = ServerSocket(CONTROL_PORT)
                server = ss
                while (!ss.isClosed) {
                    val s = ss.accept()
                    s.tcpNoDelay = true
                    Thread({ handle(s) }, "kontrol").start()
                }
            } catch (e: Exception) {
                log("Kontrol sunucusu durdu: ${e.message}")
            }
        }, "kontrol-sunucu").start()
    }

    fun stop() {
        try { server?.close() } catch (_: Exception) {}
    }

    private fun handle(s: Socket) {
        val w = OutputStreamWriter(s.getOutputStream(), Charsets.UTF_8)
        outs.add(w)
        log("PC kontrole baglandi")
        onCommand(JSONObject().put("t", "hello"))
        try {
            BufferedReader(InputStreamReader(s.getInputStream(), Charsets.UTF_8)).forEachLine { line ->
                if (line.isBlank()) return@forEachLine
                try { onCommand(JSONObject(line)) } catch (e: Exception) { log("Bozuk komut: $line") }
            }
        } catch (_: Exception) {
        } finally {
            outs.remove(w)
            try { s.close() } catch (_: Exception) {}
            onCommand(JSONObject().put("t", "stop"))
            log("PC kontrol baglantisi kapandi")
        }
    }

    private val sender = java.util.concurrent.Executors.newSingleThreadExecutor()

    fun broadcast(o: JSONObject) {
        val line = o.toString() + "\n"
        // Ag yazimi ana is parcaciginda yapilamaz
        sender.execute {
            for (w in outs) {
                try { synchronized(w) { w.write(line); w.flush() } } catch (_: Exception) { outs.remove(w) }
            }
        }
    }
}
