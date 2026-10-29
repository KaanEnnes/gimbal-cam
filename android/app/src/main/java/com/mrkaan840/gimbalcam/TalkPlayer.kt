package com.mrkaan840.gimbalcam

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Bas-konus: odadaki izleyicinin sesini telefonun hoparlorunden calar (guvenlik kamerasi gibi).
 * PCM 16 bit, 24 kHz, mono. Konusma surerken [talking] true olur; mikrofon bu sirada susar ki
 * konusan kisi kendi sesini geri duymasin (yanki).
 */
class TalkPlayer(private val log: (String) -> Unit) {
    private val q = LinkedBlockingQueue<ByteArray>(50)
    @Volatile private var lastAt = 0L
    @Volatile private var running = false
    private var track: AudioTrack? = null

    /** Son 400 ms icinde ses geldiyse konusma suruyor. */
    val talking get() = System.currentTimeMillis() - lastAt < 400

    /** Bas-konus PC'den caliniyor: telefon calmaz ama mikrofonu yine sustursun (yanki). */
    fun duck() { lastAt = System.currentTimeMillis() }

    fun feed(pcm: ByteArray) {
        lastAt = System.currentTimeMillis()
        if (!running) start()
        // Gecikme birikmesin: kuyruk doluysa en eskisini at
        if (!q.offer(pcm)) { q.poll(); q.offer(pcm) }
    }

    private fun start() {
        running = true
        Thread({
            val rate = 24000
            val min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val t = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder()
                    // MEDIA + SPEECH: ahize degil hoparlorden, yuksek sesle
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(rate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(maxOf(min, rate / 5 * 2))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            track = t
            t.play()
            log("Bas-konus: hoparlor acik")
            try {
                while (running) {
                    val d = q.poll(2, TimeUnit.SECONDS)
                    if (d == null) {
                        // 2 sn sessizlik: hoparloru birak
                        if (!talking) break
                        continue
                    }
                    t.write(d, 0, d.size)
                }
            } catch (_: Exception) {
            } finally {
                try { t.stop() } catch (_: Exception) {}
                t.release()
                track = null
                running = false
            }
        }, "bas-konus").start()
    }

    fun stop() {
        running = false
        q.clear()
    }
}
