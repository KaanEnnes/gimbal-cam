package com.mrkaan840.gimbalcam

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder

/**
 * Telefon mikrofonu: 24 kHz, mono, 16 bit PCM. 40 ms'lik parcalar [sink]'e gider.
 * PCM secildi: 384 kbps, goruntunun yaninda onemsiz; PC'de ve tarayicida kod cozucu gerektirmez.
 */
class AudioCapture(private val log: (String) -> Unit, private val sink: (ByteArray, Long) -> Unit) {
    companion object {
        const val RATE = 24000
        const val CHUNK_SAMPLES = RATE / 25  // 40 ms
    }

    @Volatile private var running = false
    private var thread: Thread? = null

    @SuppressLint("MissingPermission")
    fun start() {
        if (running) return
        running = true
        thread = Thread({
            val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val rec = try {
                // CAMCORDER: video cekimi icin ayarli mikrofon (kameranin baktigi yon, otomatik kazanc)
                AudioRecord(MediaRecorder.AudioSource.CAMCORDER, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, CHUNK_SAMPLES * 2 * 4))
            } catch (e: Exception) {
                log("Mikrofon acilamadi: ${e.message}"); running = false; return@Thread
            }
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                log("Mikrofon kullanilamiyor"); rec.release(); running = false; return@Thread
            }
            rec.startRecording()
            log("Mikrofon acik: ${RATE / 1000} kHz")
            val buf = ByteArray(CHUNK_SAMPLES * 2)
            try {
                while (running) {
                    var off = 0
                    while (off < buf.size && running) {
                        val n = rec.read(buf, off, buf.size - off)
                        if (n <= 0) break
                        off += n
                    }
                    if (off == buf.size) sink(buf.copyOf(), System.nanoTime() / 1000)
                }
            } finally {
                try { rec.stop() } catch (_: Exception) {}
                rec.release()
            }
        }, "mikrofon").apply { start() }
    }

    fun stop() {
        running = false
        thread?.join(300)
        thread = null
    }
}
