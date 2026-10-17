package com.mrkaan840.gimbalcam

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager

/**
 * Ekran kapaninca Android uygulamayi arka plana alir ve kamerayi/mikrofonu keser.
 * Bu on plan servisi (kamera + mikrofon + bagli cihaz turunde) acik oldukca kamera, mikrofon,
 * gimbal Bluetooth baglantisi ve yayin ekran kapaliyken de devam eder.
 * Isin kendisi MainActivity'deki nesnelerde; servis yalnizca sureci ve izinleri ayakta tutar.
 */
class KeepAliveService : Service() {
    private var wake: PowerManager.WakeLock? = null
    private var wifi: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("canli", "GimbalCam yayını", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(this, "canli")
            .setContentTitle("GimbalCam çalışıyor")
            .setContentText("Kamera ve gimbal ekran kapalıyken de yayında")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 30) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == android.content.pm.PackageManager.PERMISSION_GRANTED)
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            startForeground(1, n, type)
        } else {
            startForeground(1, n)
        }
        // Ekran kapaliyken islemci ve Wi-Fi uyumasin (goruntu kodlama + gonderme surer)
        if (wake == null) {
            wake = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GimbalCam:yayin").apply { setReferenceCounted(false); acquire() }
        }
        if (wifi == null) {
            @Suppress("DEPRECATION")
            wifi = getSystemService(WifiManager::class.java)
                .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "GimbalCam:ekran-kapali")
                .apply { setReferenceCounted(false); acquire() }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        try { wake?.release() } catch (_: Exception) {}
        try { wifi?.release() } catch (_: Exception) {}
        super.onDestroy()
    }
}
