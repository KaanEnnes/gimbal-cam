package com.mrkaan840.gimbalcam

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.MotionEvent
import android.view.OrientationEventListener
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.TextView
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface

class MainActivity : Activity() {

    private val main = Handler(Looper.getMainLooper())
    private lateinit var status: TextView
    private lateinit var logView: TextView
    private lateinit var surface: SurfaceView
    private val logLines = ArrayDeque<String>()

    private lateinit var gimbal: GimbalBle
    private lateinit var camera: CameraStreamer
    private lateinit var video: VideoServer
    private lateinit var control: ControlServer
    private var started = false

    // Gimbal hiz durumu
    @Volatile private var moveX = 0
    @Volatile private var moveY = 0
    @Volatile private var lastMoveAt = 0L
    private var localSpeed = 300
    @Volatile private var localHeld = false
    private var lastStatusAt = 0L
    private var lastEncoded = -1L
    private var lastEncodedAt = 0L
    private var lastRestartAt = 0L
    private var wifiLock: WifiManager.WifiLock? = null

    // Telefonun gercek yonu (gimbal dikey moda gecince degisir). Uygulama ekrani yatay kilitli;
    // PC goruntuyu bu kadar saat yonunde dondurur.
    @Volatile private var rotation = 0
    private val orientation by lazy {
        object : OrientationEventListener(this) {
            override fun onOrientationChanged(deg: Int) {
                if (deg == OrientationEventListener.ORIENTATION_UNKNOWN) return
                // Yatay kilitli ekranin dogal yonu 270 derece; aradaki fark kadar donmus demektir
                val rel = ((deg - 270) % 360 + 360) % 360
                val snapped = ((rel + 45) / 90 % 4) * 90
                // Histerezis: kose bolgelerinde (±20 derece) karar degistirme
                val dist = kotlin.math.abs(((rel - snapped + 540) % 360) - 180)
                if (snapped != rotation && dist > 160) {
                    rotation = snapped
                    log("Yon: $rotation derece")
                    main.post { sendStatus() }
                }
            }
        }
    }

    // Uyarlanir bit hizi: ag yetismezse dusur, rahatlayinca hedefe dogru geri cik
    @Volatile private var targetBitrate = 6_000_000
    @Volatile private var currentBitrate = 6_000_000
    @Volatile private var lastCongestionAt = 0L
    private var lastRaiseAt = 0L

    private fun onCongestion() {
        val now = System.currentTimeMillis()
        if (now - lastCongestionAt < 1000) return
        lastCongestionAt = now
        currentBitrate = (currentBitrate * 0.7).toInt().coerceAtLeast(1_000_000)
        camera.setBitrate(currentBitrate)
        log("Ag yavas: bit hizi ${currentBitrate / 100_000 / 10.0} Mbps")
    }

    private fun log(s: String) {
        android.util.Log.i("GimbalCam", s)
        main.post {
            logLines.addLast(s)
            while (logLines.size > 6) logLines.removeFirst()
            if (::logView.isInitialized) logView.text = logLines.joinToString("\n")
            refreshStatus()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        buildUi()

        gimbal = GimbalBle(this, ::log).also { it.onState = { main.post { refreshStatus(); sendStatus() } } }
        video = VideoServer(::log, { camera.requestKeyFrame() }, ::onCongestion)
        camera = CameraStreamer(this, ::log) { d, pts, f -> video.push(d, pts, f) }
        control = ControlServer(::log, ::onCommand)

        askPermissions()
        main.postDelayed(ticker, 100)
    }

    private fun askPermissions() {
        val perms = mutableListOf(Manifest.permission.CAMERA)
        if (Build.VERSION.SDK_INT >= 31) {
            perms += Manifest.permission.BLUETOOTH_SCAN
            perms += Manifest.permission.BLUETOOTH_CONNECT
        } else {
            perms += Manifest.permission.ACCESS_FINE_LOCATION
        }
        val missing = perms.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isEmpty()) startAll() else requestPermissions(missing.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) startAll()
        else log("Izin verilmedi: kamera ve yakindaki cihazlar izni gerekli")
    }

    private fun startAll() {
        if (started) return
        started = true
        // Wi-Fi guc tasarrufu paketleri geciktirir: dusuk gecikme kilidi al
        wifiLock = getSystemService(WifiManager::class.java)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "GimbalCam")
            .apply { setReferenceCounted(false); acquire() }
        if (orientation.canDetectOrientation()) orientation.enable()
        currentBitrate = camera.settings.bitrate
        targetBitrate = camera.settings.bitrate
        video.start()
        control.start()
        gimbal.start()
        if (surface.holder.surface?.isValid == true) camera.start(surface.holder.surface)
        refreshStatus()
    }

    override fun onDestroy() {
        super.onDestroy()
        main.removeCallbacksAndMessages(null)
        try { wifiLock?.release() } catch (_: Exception) {}
        orientation.disable()
        gimbal.stop()
        camera.stop()
        video.stop()
        control.stop()
    }

    // ---------- Gimbal hareketi ----------

    private val ticker = object : Runnable {
        override fun run() {
            val now = System.currentTimeMillis()
            if (localHeld) lastMoveAt = now
            if (now - lastStatusAt > 2000) { lastStatusAt = now; sendStatus(); refreshStatus() }
            if (currentBitrate < targetBitrate && now - lastCongestionAt > 5000 && now - lastRaiseAt > 3000) {
                lastRaiseAt = now
                currentBitrate = (currentBitrate * 1.15).toInt().coerceAtMost(targetBitrate)
                camera.setBitrate(currentBitrate)
            }
            // Kamera bekcisi: kare uretimi durduysa (baska uygulama kamerayi aldiysa vb.) yeniden baslat
            if (started) {
                val enc = camera.encoded
                if (enc != lastEncoded) { lastEncoded = enc; lastEncodedAt = now }
                else if (now - lastEncodedAt > 3000 && now - lastRestartAt > 5000) {
                    lastRestartAt = now
                    log("Kamera durmus, yeniden baslatiliyor")
                    camera.setPreview(if (surface.holder.surface?.isValid == true) surface.holder.surface else null)
                }
            }
            if ((moveX != 0 || moveY != 0)) {
                if (now - lastMoveAt > 500) {
                    // PC'den komut gelmiyor: guvenlik icin dur
                    moveX = 0; moveY = 0
                    gimbal.send(GimbalBle.move(0, 0))
                } else {
                    gimbal.send(GimbalBle.move(moveX, moveY))
                }
            }
            main.postDelayed(this, 100)
        }
    }

    private fun setMove(x: Int, y: Int) {
        val wasMoving = moveX != 0 || moveY != 0
        moveX = x; moveY = y
        lastMoveAt = System.currentTimeMillis()
        if (x == 0 && y == 0) {
            if (wasMoving) gimbal.send(GimbalBle.move(0, 0))
        } else if (!wasMoving) {
            gimbal.send(GimbalBle.move(x, y))
        }
    }

    private fun onCommand(o: JSONObject) {
        when (o.optString("t")) {
            "hello" -> main.post { sendStatus() }
            "move" -> setMove(o.optInt("x"), o.optInt("y"))
            "stop" -> setMove(0, 0)
            "center" -> { setMove(0, 0); gimbal.send(GimbalBle.angle(0, 0, 0)) }
            "raw" -> {
                // Deneme icin: {"t":"raw","type":3,"hex":"AA55..."}
                val hex = o.optString("hex").replace(" ", "")
                val bytes = ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
                gimbal.send(GimbalBle.wrap(o.optInt("type", 3), bytes))
            }
            "zoom" -> camera.setZoom(o.optDouble("v", 1.0).toFloat())
            "focus" -> camera.focusAt(o.optDouble("x", 0.5).toFloat(), o.optDouble("y", 0.5).toFloat())
            "autofocus" -> camera.autoFocus()
            "ev" -> camera.setExposure(o.optInt("v"))
            "torch" -> camera.setTorch(o.optBoolean("v"))
            "bitrate" -> {
                targetBitrate = o.optInt("v", 6_000_000)
                currentBitrate = targetBitrate
                camera.setBitrate(targetBitrate)
            }
            "camera" -> { camera.settings.cameraId = o.optString("id", "0"); camera.restart(); main.postDelayed({ sendStatus() }, 800) }
            "res" -> {
                camera.settings.width = o.optInt("w", 1280)
                camera.settings.height = o.optInt("h", 720)
                camera.settings.fps = o.optInt("fps", 30)
                camera.restart()
                main.postDelayed({ sendStatus() }, 800)
            }
        }
    }

    private fun sendStatus() {
        val s = camera.settings
        control.broadcast(
            JSONObject()
                .put("t", "status")
                .put("gimbal", gimbal.state)
                .put("gimbalConnected", gimbal.connected)
                .put("camera", s.cameraId)
                .put("rot", rotation)
                .put("front", camera.isFront())
                .put("cameras", org.json.JSONArray(camera.cameraList()))
                .put("w", s.width).put("h", s.height).put("fps", s.fps).put("bitrate", s.bitrate)
        )
    }

    // ---------- Arayuz ----------

    private fun wifiIp(): String {
        try {
            for (ni in NetworkInterface.getNetworkInterfaces()) {
                if (!ni.isUp || ni.isLoopback) continue
                for (a in ni.inetAddresses) if (a is Inet4Address && !a.isLoopbackAddress) return a.hostAddress ?: ""
            }
        } catch (_: Exception) {}
        return "?"
    }

    private fun refreshStatus() {
        if (!::status.isInitialized || !::gimbal.isInitialized) return
        val s = camera.settings
        status.text = "Gimbal: ${gimbal.state}\n" +
            "Kamera ${s.cameraId}: ${s.width}x${s.height} ${s.fps}fps\n" +
            "PC: ${if (video.clientCount > 0) "bagli" else "bekleniyor"}  |  Wi-Fi IP: ${wifiIp()}\n" +
            "Kodlanan kare: ${camera.encoded}  |  Oturum: ${if (camera.isOpen) "acik" else "kapali"}"
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun holdButton(label: String, dx: Int, dy: Int): Button = Button(this).apply {
        text = label
        textSize = 20f
        setOnTouchListener { _, e ->
            when (e.action) {
                MotionEvent.ACTION_DOWN -> { localHeld = true; setMove(dx * localSpeed, dy * localSpeed) }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> { localHeld = false; setMove(0, 0) }
                MotionEvent.ACTION_MOVE -> lastMoveAt = System.currentTimeMillis()
            }
            false
        }
    }

    private fun buildUi() {
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        surface = SurfaceView(this)
        surface.holder.addCallback(object : SurfaceHolder.Callback {
            override fun surfaceCreated(h: SurfaceHolder) { if (started) camera.setPreview(h.surface) else Unit }
            override fun surfaceChanged(h: SurfaceHolder, f: Int, w: Int, hh: Int) {}
            override fun surfaceDestroyed(h: SurfaceHolder) { if (started) camera.setPreview(null) }
        })
        root.addView(surface, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))

        status = TextView(this).apply {
            setTextColor(Color.WHITE)
            setBackgroundColor(0x88000000.toInt())
            setPadding(16, 8, 16, 8)
            textSize = 13f
        }
        root.addView(status, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP or Gravity.START))

        logView = TextView(this).apply {
            setTextColor(0xFFB0FFB0.toInt())
            setBackgroundColor(0x66000000)
            setPadding(16, 8, 16, 8)
            textSize = 11f
        }
        root.addView(logView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP or Gravity.END))

        val pad = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(0x66000000)
            gravity = Gravity.CENTER_VERTICAL
            addView(holdButton("◀", -1, 0))
            addView(holdButton("▲", 0, 1))
            addView(holdButton("▼", 0, -1))
            addView(holdButton("▶", 1, 0))
            addView(Button(this@MainActivity).apply {
                text = "Ortala"
                setOnClickListener { onCommand(JSONObject().put("t", "center")) }
            })
            addView(Button(this@MainActivity).apply {
                text = "Kamera"
                setOnClickListener {
                    val ids = camera.cameraList().map { it.substringBefore(":") }
                    val i = ids.indexOf(camera.settings.cameraId)
                    camera.settings.cameraId = ids[(i + 1) % ids.size]
                    camera.restart()
                    main.postDelayed({ refreshStatus() }, 800)
                }
            })
            addView(TextView(this@MainActivity).apply { text = "  Hiz"; setTextColor(Color.WHITE) })
            addView(SeekBar(this@MainActivity).apply {
                max = 2000
                progress = localSpeed
                setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                    override fun onProgressChanged(s: SeekBar, p: Int, u: Boolean) { localSpeed = p.coerceAtLeast(10) }
                    override fun onStartTrackingTouch(s: SeekBar) {}
                    override fun onStopTrackingTouch(s: SeekBar) {}
                })
            }, LinearLayout.LayoutParams(300, LinearLayout.LayoutParams.WRAP_CONTENT))
        }
        root.addView(pad, FrameLayout.LayoutParams(FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL))

        setContentView(root)
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_FULLSCREEN or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
    }
}
