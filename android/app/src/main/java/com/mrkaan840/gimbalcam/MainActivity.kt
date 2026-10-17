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
    private lateinit var audio: AudioCapture
    private var started = false

    // Gimbal hiz durumu
    @Volatile private var moveX = 0
    @Volatile private var moveY = 0
    @Volatile private var lastMoveAt = 0L
    private var localSpeed = 300
    @Volatile private var localHeld = false
    private var lastStatusAt = 0L
    private var lastTouchAt = System.currentTimeMillis()
    private var dimmed = false

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        lastTouchAt = System.currentTimeMillis()
        return super.dispatchTouchEvent(ev)
    }
    private var lastEncoded = -1L
    private var lastEncodedAt = 0L
    private var lastRestartAt = 0L
    private var wifiLock: WifiManager.WifiLock? = null

    // Telefonun gercek yonu (gimbal dikey moda gecince degisir). Uygulama ekrani yatay kilitli;
    // PC goruntuyu bu kadar saat yonunde dondurur.
    @Volatile private var rotation = 0
    private var lastTiltAt = 0L
    @Volatile private var lastResidual = 0.0
    @Volatile private var tiltWanted = false

    // Ufuk kilidi: yercekimi sensoru (ivmeolcer + jiroskop birlesik, yumusak) ile ondalikli egiklik
    private val gravityListener = object : android.hardware.SensorEventListener {
        override fun onSensorChanged(e: android.hardware.SensorEvent) {
            val gx = e.values[0].toDouble(); val gy = e.values[1].toDouble(); val gz = e.values[2].toDouble()
            // Telefon yatay (masada duz) ise egiklik anlamsiz
            if (kotlin.math.abs(gz) > 8.5) return
            // OrientationEventListener ile ayni olcu: saat yonunde artan derece (0 = dik portre)
            var o = 90.0 - Math.toDegrees(kotlin.math.atan2(-gy, gx))
            o = ((o % 360) + 360) % 360
            var res = ((o - 270 - rotation) % 360 + 360) % 360
            if (res > 180) res -= 360
            lastResidual = res
            val now = System.currentTimeMillis()
            if (tiltWanted && now - lastTiltAt > 50) {
                lastTiltAt = now
                control.broadcast(JSONObject().put("t", "tilt").put("a", Math.round(res * 10) / 10.0))
            }
        }
        override fun onAccuracyChanged(s: android.hardware.Sensor, a: Int) {}
    }
    @Volatile private var gimbalMode = 1
    private val orientation by lazy {
        object : OrientationEventListener(this) {
            override fun onOrientationChanged(deg: Int) {
                if (deg == OrientationEventListener.ORIENTATION_UNKNOWN) return
                // Yatay kilitli ekranin dogal yonu 270 derece; aradaki fark kadar donmus demektir
                val rel = ((deg - 270) % 360 + 360) % 360
                val snapped = ((rel + 45) / 90 % 4) * 90

                // Histerezis: yeni yone ancak ±25 derece yaklasinca gec (kose bolgelerinde titremesin)
                val dist = kotlin.math.abs(((rel - snapped + 540) % 360) - 180)
                if (snapped != rotation && dist < 25) {
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
        // Telefon gimbal uzerinde kilitliyken de calissin: kilit ekraninin ustunde acil, ekrani yak
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(true); setTurnScreenOn(true) }
        else @Suppress("DEPRECATION") window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        buildUi()

        gimbal = GimbalBle(this, ::log).also {
            it.onState = { main.post { refreshStatus(); sendStatus() } }
            it.onNotify = ::onGimbalNotify
        }
        video = VideoServer(::log, { camera.requestKeyFrame() }, ::onCongestion)
        camera = CameraStreamer(this, ::log) { d, pts, f -> video.push(d, pts, f) }
        control = ControlServer(::log, ::onCommand)
        audio = AudioCapture(::log) { d, pts -> video.pushAudio(d, pts) }

        askPermissions()
        main.postDelayed(ticker, 100)
    }

    private fun askPermissions() {
        val perms = mutableListOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) perms += Manifest.permission.POST_NOTIFICATIONS
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
        // Mikrofon izni verilmese de goruntu ve gimbal calissin
        val camOk = checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        if (camOk) startAll() else log("Izin verilmedi: kamera ve yakindaki cihazlar izni gerekli")
    }

    private fun startAll() {
        if (started) return
        started = true
        // Wi-Fi guc tasarrufu paketleri geciktirir: dusuk gecikme kilidi al
        wifiLock = getSystemService(WifiManager::class.java)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "GimbalCam")
            .apply { setReferenceCounted(false); acquire() }
        // Ekran kapaninca da yayin surmesi icin on plan servisi (uygulama on plandayken baslatilmali)
        try { startForegroundService(android.content.Intent(this, KeepAliveService::class.java)) }
        catch (e: Exception) { log("Arka plan servisi baslatilamadi: ${e.message}") }
        if (orientation.canDetectOrientation()) orientation.enable()
        val sm = getSystemService(android.hardware.SensorManager::class.java)
        (sm.getDefaultSensor(android.hardware.Sensor.TYPE_GRAVITY) ?: sm.getDefaultSensor(android.hardware.Sensor.TYPE_ACCELEROMETER))
            ?.let { sm.registerListener(gravityListener, it, android.hardware.SensorManager.SENSOR_DELAY_GAME) }
        currentBitrate = camera.settings.bitrate
        targetBitrate = camera.settings.bitrate
        video.start()
        control.start()
        gimbal.start()
        // Mikrofon, PC biri dinliyor dediginde acilir ("audio" komutu); kimse dinlemiyorsa bant genisligi harcamaz
        if (surface.holder.surface?.isValid == true) camera.start(surface.holder.surface)
        refreshStatus()
    }

    override fun onDestroy() {
        super.onDestroy()
        main.removeCallbacksAndMessages(null)
        try { wifiLock?.release() } catch (_: Exception) {}
        stopService(android.content.Intent(this, KeepAliveService::class.java))
        orientation.disable()
        getSystemService(android.hardware.SensorManager::class.java).unregisterListener(gravityListener)
        gimbal.stop()
        audio.stop()
        camera.stop()
        video.stop()
        control.stop()
    }

    // ---------- Gimbal hareketi ----------

    private val ticker = object : Runnable {
        override fun run() {
            val now = System.currentTimeMillis()
            if (localHeld) lastMoveAt = now
            // 30 sn dokunulmazsa ekrani karart (pil); dokununca geri gelir
            val dim = now - lastTouchAt > 30_000
            if (dim != dimmed) {
                dimmed = dim
                window.attributes = window.attributes.apply { screenBrightness = if (dim) 0.01f else -1f }
            }
            if (now - lastStatusAt > 2000) { lastStatusAt = now; sendStatus(); refreshStatus() }
            // Telefon sabitken dinleyici yeni deger vermez: son egikligi yarim saniyede bir tekrarla
            if (tiltWanted && now - lastTiltAt > 500) {
                lastTiltAt = now
                control.broadcast(JSONObject().put("t", "tilt").put("a", lastResidual))
            }
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
            "bye" -> { setMove(0, 0); audio.stop(); tiltWanted = false }
            "center" -> { setMove(0, 0); gimbal.send(GimbalBle.angle(0, 0, 0)) }
            "gmode" -> {
                // 1 = Pan/Tilt takip, 2 = Yatay takip, 3 = Tum eksenler kilitli, 4 = POV
                val m = o.optInt("v", 1).coerceIn(1, 4)
                gimbal.send(GimbalBle.wrap(3, byteArrayOf(0xAA.toByte(), 0x55, 0x00, 0x02, 0x25, 0x01, m.toByte())))
                gimbalMode = m
                main.post { sendStatus() }
            }
            "raw" -> {
                // Deneme icin: {"t":"raw","type":3,"hex":"AA55..."}
                val hex = o.optString("hex").replace(" ", "")
                val bytes = ByteArray(hex.length / 2) { hex.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
                gimbal.send(GimbalBle.wrap(o.optInt("type", 3), bytes))
            }
            "audio" -> {
                val on = o.optBoolean("v")
                if (on && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) audio.start()
                else if (!on) audio.stop()
            }
            "tilt" -> tiltWanted = o.optBoolean("v")
            "keyframe" -> camera.requestKeyFrame()
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

    // Gimbal durum paketleri: ayni komut turunden en fazla 2/sn PC'ye gonder
    private val lastNotify = HashMap<String, Long>()
    // Gimbal durum paketi (komut 0x94): [pil seviyesi][durum]; durumda 0x08 biti = sarjda (olculdu: 0x02 pilde, 0x0b sarjda)
    @Volatile private var gBattery = -1
    @Volatile private var gCharging: Boolean? = null

    private fun onGimbalNotify(v: ByteArray) {
        val hex = v.joinToString("") { "%02x".format(it) }
        val i = hex.indexOf("aa55000394")
        if (i >= 0 && hex.length >= i + 14) {
            val bat = hex.substring(i + 10, i + 12).toInt(16)
            val charging = hex.substring(i + 12, i + 14).toInt(16) and 0x08 != 0
            if (bat != gBattery || charging != gCharging) {
                gBattery = bat; gCharging = charging
                log("Gimbal pil: $bat${if (charging) " (sarjda)" else ""}")
                main.post { sendStatus(); refreshStatus() }
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
                .put("drops", gimbal.drops)
                .put("gBattery", gBattery)
                .put("gCharging", gCharging == true)
                .put("dropReason", gimbal.lastDropStatus)
                .put("camera", s.cameraId)
                .put("rot", rotation)
                .put("gmode", gimbalMode)
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
