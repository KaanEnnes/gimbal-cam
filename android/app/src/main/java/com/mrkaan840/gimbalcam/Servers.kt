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
/**
 * Istemci basina tolerans (~0.5 sn goruntu). Asilinca kuyruk bosaltilip anahtar kare beklenir; bu telefonda
 * istenen anahtar kare ~0.6 sn surdugu icin her tasma gorunur bir donma demek. Anlik yavaslamalari
 * (Wi-Fi dalgalanmasi, PC'nin bir an mesgul olmasi) kare atlatmadan kisa bir gecikmeyle atlatmak daha iyi.
 */
const val QUEUE_FRAMES = 15

/**
 * Goruntu sunucusu. Her kare su sekilde gider:
 *   uzunluk(int32) | pts mikrosaniye(int64) | bayraklar(int8) | H.264 Annex-B verisi
 * Bayrak 2 = codec ayari (SPS/PPS), 1 = anahtar kare, 4 = ses (PCM 16 bit, 24 kHz, mono), 8 = HEVC (yoksa H.264).
 */
class VideoServer(
    private val log: (String) -> Unit,
    private val onClient: () -> Unit,
    private val onCongestion: () -> Unit,
) {
    private class Client(val sock: Socket) {
        val q = ArrayBlockingQueue<Triple<ByteArray, Long, Int>>(QUEUE_FRAMES * 3)
        @Volatile var needKey = true
        @Volatile var lastSentAt = System.currentTimeMillis()
    }

    private val clients = CopyOnWriteArrayList<Client>()
    @Volatile private var config: ByteArray? = null
    @Volatile private var configFlags = 2
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
        if (isConfig) {
            // Cozunurluk/lens degisince kodlayici yeniden baslar ve yeni SPS/PPS gonderir. Bagli istemciler
            // bunu almazsa yeni kareleri eski ayarlarla cozer -> renkli blok copu. Herkesi yeni ayar +
            // anahtar kareden baslat.
            val old = config
            if (old == null || !old.contentEquals(data)) clients.forEach { it.needKey = true }
            config = data
            configFlags = 2 or (flags and 8)
            return
        }
        val f = (if (isKey) 1 else 0) or (flags and 8)
        for (c in clients) {
            if (c.needKey) {
                if (!isKey) continue
                val cfg = config ?: continue
                c.q.clear()
                c.q.offer(Triple(cfg, pts, configFlags))
                c.needKey = false
            }
            // Kuyrukta ses de var: yalnizca goruntu birikimi ~8 kareyi asinca tikanma say
            if (c.q.size >= QUEUE_FRAMES * 2 || !c.q.offer(Triple(data, pts, f))) {
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

    fun pushAudio(data: ByteArray, pts: Long) {
        for (c in clients) {
            // Goruntu senkron olunca ses de akar; kuyruk doluysa ses parcasini at (gecikme biriktirme)
            if (!c.needKey && c.q.remainingCapacity() > 0) c.q.offer(Triple(data, pts, 4))
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
            // Son PC de gittiyse ses/egim gibi istege bagli akislari kapat
            if (outs.isEmpty()) onCommand(JSONObject().put("t", "bye"))
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
