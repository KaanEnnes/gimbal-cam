package com.mrkaan840.gimbalcam

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Rect
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.MeteringRectangle
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Range
import android.util.Size
import android.view.Surface

/**
 * Camera2 ile kamerayi acar, goruntuyu hem ekrandaki onizlemeye hem de
 * donanim H.264 kodlayicisina verir. Kodlanmis kareler [sink]'e gider.
 */
class CameraStreamer(
    private val ctx: Context,
    private val log: (String) -> Unit,
    private val sink: (data: ByteArray, ptsUs: Long, flags: Int) -> Unit,
) {
    data class Settings(
        var cameraId: String = "0", var width: Int = 1280, var height: Int = 720, var fps: Int = 30,
        var bitrate: Int = 6_000_000,
        /** "avc" (H.264) ya da "hevc" (H.265: ayni bant genisliginde daha temiz, oda tarayicilari cozemez) */
        var codec: String = "avc",
    )

    val settings = Settings()
    private val manager = ctx.getSystemService(CameraManager::class.java)
    private val thread = HandlerThread("kamera").apply { start() }
    private val handler = Handler(thread.looper)
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var encoder: MediaCodec? = null
    private var encoderSurface: Surface? = null
    private var preview: Surface? = null
    private var request: CaptureRequest.Builder? = null
    @Volatile private var running = false
    // Her kapatmada artar; eski kamera geri cagrilari kendi nesillerini gorunce hicbir sey yapmaz
    private var generation = 0
    @Volatile var encoded = 0L
        private set
    val isOpen get() = session != null
    private var drainThread: Thread? = null

    // Canli ayarlar
    private var zoom = 1f
    private var exposure = 0
    private var torch = false
    private var afRegion: MeteringRectangle? = null

    fun isFront() = try {
        manager.getCameraCharacteristics(settings.cameraId).get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_FRONT
    } catch (_: Exception) { false }

    /** Kamera is parcacigina is gonder; hata uygulamayi cokertmesin (cokme = gimbal baglantisi kopar). */
    private fun safePost(block: () -> Unit) = handler.post {
        try { block() } catch (e: Exception) { log("Kamera islemi hatasi: ${e.message}") }
    }

    fun cameraList(): List<String> = manager.cameraIdList.map { id ->
        val c = manager.getCameraCharacteristics(id)
        val facing = when (c.get(CameraCharacteristics.LENS_FACING)) {
            CameraCharacteristics.LENS_FACING_FRONT -> "on"
            CameraCharacteristics.LENS_FACING_BACK -> "arka"
            else -> "harici"
        }
        "$id:$facing"
    }

    fun start(previewSurface: Surface?) {
        preview = previewSurface
        safePost { openCamera() }
    }

    fun restart() {
        safePost {
            closeAll()
            openCamera()
        }
    }

    fun stop() {
        safePost { closeAll() }
    }

    fun setPreview(s: Surface?) {
        preview = s
        restart()
    }

    private fun pickSize(c: CameraCharacteristics): Size {
        val map = c.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)!!
        val sizes = map.getOutputSizes(MediaCodec::class.java) ?: map.getOutputSizes(Surface::class.java)
        val want = settings.width * settings.height
        return sizes.filter { it.width * 9 == it.height * 16 }.minByOrNull { kotlin.math.abs(it.width * it.height - want) }
            ?: sizes.minBy { kotlin.math.abs(it.width * it.height - want) }
    }

    @SuppressLint("MissingPermission")
    private fun openCamera() {
        try {
            val c = manager.getCameraCharacteristics(settings.cameraId)
            val size = pickSize(c)
            settings.width = size.width
            settings.height = size.height
            setupEncoder(size)
            val gen = generation
            manager.openCamera(settings.cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) {
                    if (gen != generation) { d.close(); return }
                    device = d
                    try { createSession(gen) } catch (e: Exception) { log("Kamera oturumu hatasi: ${e.message}") }
                }
                override fun onDisconnected(d: CameraDevice) {
                    d.close()
                    if (device === d) device = null
                }
                override fun onError(d: CameraDevice, error: Int) {
                    log("Kamera hatasi: $error")
                    d.close()
                    if (device === d) device = null
                }
            }, handler)
        } catch (e: Exception) {
            log("Kamera acilamadi: ${e.message}")
        }
    }

    private fun setupEncoder(size: Size) {
        val mime = if (settings.codec == "hevc") MediaFormat.MIMETYPE_VIDEO_HEVC else MediaFormat.MIMETYPE_VIDEO_AVC
        val fmt = MediaFormat.createVideoFormat(mime, size.width, size.height).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, settings.bitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, settings.fps)
            // Anahtar kare buyuk bir paket; zayif Wi-Fi'de periyodik sicrama kuyrugu tasirir.
            // 3 sn'de bir yeter: yeni izleyici ve tikanmada zaten aninda isteniyor.
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 3)
            setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR)
            // Dusuk gecikme: B-kare yok, gecikmeli cikti yok
            setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0)
            if (Build.VERSION.SDK_INT >= 30) setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            setInteger(MediaFormat.KEY_PRIORITY, 0)
        }
        val enc = MediaCodec.createEncoderByType(mime)
        enc.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        encoderSurface = enc.createInputSurface()
        enc.start()
        encoder = enc
        running = true
        drainThread = Thread({ drain(enc) }, "kodlayici").apply { start() }
        log("Kodlayici: ${if (settings.codec == "hevc") "HEVC" else "H.264"} ${size.width}x${size.height} ${settings.fps}fps ${settings.bitrate / 1_000_000}Mbps")
    }

    private fun drain(enc: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        while (running) {
            val idx = try { enc.dequeueOutputBuffer(info, 10_000) } catch (_: Exception) { break }
            if (idx < 0) continue
            val buf = enc.getOutputBuffer(idx)
            if (buf != null && info.size > 0) {
                val out = ByteArray(info.size)
                buf.position(info.offset)
                buf.get(out)
                encoded++
                // Yalnizca anahtar/ayar bitleri; 8 = HEVC isareti (PC dogru cozucuyu secsin)
                val f = (info.flags and (MediaCodec.BUFFER_FLAG_KEY_FRAME or MediaCodec.BUFFER_FLAG_CODEC_CONFIG)) or
                    (if (settings.codec == "hevc") 8 else 0)
                sink(out, info.presentationTimeUs, f)
            }
            try { enc.releaseOutputBuffer(idx, false) } catch (_: Exception) { break }
        }
    }

    private fun createSession(gen: Int) {
        val d = device ?: return
        val targets = listOfNotNull(encoderSurface, preview)
        @Suppress("DEPRECATION")
        d.createCaptureSession(targets, object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                // Bu arada kamera kapatilip yeniden acildiysa bu eski oturumdur: dokunma
                if (gen != generation || device !== d) { try { s.close() } catch (_: Exception) {}; return }
                try {
                    configure(d, s, targets)
                } catch (e: Exception) {
                    log("Kamera ayarlanamadi: ${e.message}")
                }
            }
            override fun onConfigureFailed(s: CameraCaptureSession) { log("Kamera oturumu kurulamadi") }
        }, handler)
    }

    private fun configure(d: CameraDevice, s: CameraCaptureSession, targets: List<Surface>) {
                session = s
                // Bazi on kameralar kayit sablonunu desteklemiyor: onizleme sablonuna dus
                val builder = try { d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD) }
                    catch (_: Exception) { d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW) }
                request = builder.apply {
                    targets.forEach { addTarget(it) }
                    set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO)
                    set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
                    set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(settings.fps, settings.fps))
                    set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
                }
                applyLive()
                log("Kamera acik: ${settings.cameraId}")
    }

    private fun chars() = manager.getCameraCharacteristics(settings.cameraId)

    private fun applyLive() {
        val r = request ?: return
        val s = session ?: return
        val c = chars()
        if (Build.VERSION.SDK_INT >= 30 && c.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE) != null) {
            val range = c.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE)!!
            r.set(CaptureRequest.CONTROL_ZOOM_RATIO, zoom.coerceIn(range.lower, range.upper))
        } else {
            val active = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)!!
            val maxZ = c.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM) ?: 1f
            val z = zoom.coerceIn(1f, maxZ)
            val w = (active.width() / z).toInt()
            val h = (active.height() / z).toInt()
            r.set(CaptureRequest.SCALER_CROP_REGION, Rect((active.width() - w) / 2, (active.height() - h) / 2, (active.width() + w) / 2, (active.height() + h) / 2))
        }
        val evRange = c.get(CameraCharacteristics.CONTROL_AE_COMPENSATION_RANGE)
        if (evRange != null) r.set(CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION, exposure.coerceIn(evRange.lower, evRange.upper))
        r.set(CaptureRequest.FLASH_MODE, if (torch) CaptureRequest.FLASH_MODE_TORCH else CaptureRequest.FLASH_MODE_OFF)
        afRegion?.let {
            r.set(CaptureRequest.CONTROL_AF_REGIONS, arrayOf(it))
            r.set(CaptureRequest.CONTROL_AE_REGIONS, arrayOf(it))
        }
        try { s.setRepeatingRequest(r.build(), null, handler) } catch (e: Exception) { log("Istek hatasi: ${e.message}") }
    }

    fun setZoom(z: Float) = safePost { zoom = z; applyLive() }
    fun setExposure(ev: Int) = safePost { exposure = ev; applyLive() }
    fun setTorch(on: Boolean) = safePost { torch = on; applyLive() }

    /** x,y: 0..1 goruntu koordinati. Oraya odaklan. */
    fun focusAt(x: Float, y: Float) = safePost {
        val active = chars().get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE) ?: return@safePost
        val cx = (x.coerceIn(0f, 1f) * active.width()).toInt()
        val cy = (y.coerceIn(0f, 1f) * active.height()).toInt()
        val half = active.width() / 10
        afRegion = MeteringRectangle(
            (cx - half).coerceAtLeast(0), (cy - half).coerceAtLeast(0),
            half * 2, half * 2, MeteringRectangle.METERING_WEIGHT_MAX - 1,
        )
        val r = request ?: return@safePost
        r.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_AUTO)
        applyLive()
        r.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_START)
        try { session?.capture(r.build(), null, handler) } catch (_: Exception) {}
        r.set(CaptureRequest.CONTROL_AF_TRIGGER, CaptureRequest.CONTROL_AF_TRIGGER_IDLE)
    }

    fun autoFocus() = safePost {
        afRegion = null
        request?.apply {
            set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
            set(CaptureRequest.CONTROL_AF_REGIONS, null)
            set(CaptureRequest.CONTROL_AE_REGIONS, null)
        }
        applyLive()
    }

    fun requestKeyFrame() {
        try {
            encoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) })
        } catch (_: Exception) {}
    }

    fun setBitrate(bps: Int) {
        settings.bitrate = bps
        try {
            encoder?.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, bps) })
        } catch (_: Exception) {}
    }

    private fun closeAll() {
        generation++
        running = false
        try { session?.close() } catch (_: Exception) {}
        session = null
        try { device?.close() } catch (_: Exception) {}
        device = null
        drainThread?.join(500)
        drainThread = null
        try { encoder?.stop() } catch (_: Exception) {}
        try { encoder?.release() } catch (_: Exception) {}
        encoder = null
        encoderSurface?.release()
        encoderSurface = null
        request = null
    }
}
