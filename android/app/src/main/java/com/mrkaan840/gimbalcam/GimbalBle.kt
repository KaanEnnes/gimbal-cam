package com.mrkaan840.gimbalcam

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Handler
import android.os.Looper
import java.util.UUID

/**
 * Merak One M1 gimbal'i ile BLE konusmasi.
 * Protokol GimbalPlay (com.netviv.photo.gimbalplay 2.5.55) uygulamasindan cikarildi:
 *   zarf   : FF FF | uzunluk(2 bayt, big-endian, = payload+2) | tip | payload | crc8
 *   crc8   : uzunluk..payload uzerinden, polinom 0x8D, baslangic 0
 *   el sik.: tip 1, bos payload
 *   hareket: tip 3, payload = AA 55 00 05 20 | yatayHiz int16 LE | dikeyHiz int16 LE
 *   aci    : tip 3, payload = AA 55 00 08 22 02 | roll*10 | pitch*10 | yaw*10 (int16 LE)
 */
@SuppressLint("MissingPermission")
class GimbalBle(private val ctx: Context, private val log: (String) -> Unit) {

    companion object {
        private fun u(s: String): UUID = UUID.fromString(s)
        val NAME_PREFIXES = listOf("M1-A", "M1", "Anko43635047", "M2-A", "M2-B")
        // GimbalPlay'in tanidigi servis/karakteristik ciftleri (servis, yazma, bildirim)
        val CHANNELS = listOf(
            Triple(u("0000fff0-0000-1000-8000-00805f9b34fb"), u("0000fff2-0000-1000-8000-00805f9b34fb"), u("0000fff1-0000-1000-8000-00805f9b34fb")),
            Triple(u("0000ffe0-0000-1000-8000-00805f9b34fb"), u("0000ffe1-0000-1000-8000-00805f9b34fb"), u("0000ffe2-0000-1000-8000-00805f9b34fb")),
            Triple(u("22b3a000-2617-f8c9-d28a-6728eaeaeaae"), u("22b3a004-2617-f8c9-d28a-6728eaeaeaae"), u("22b3a007-2617-f8c9-d28a-6728eaeaeaae")),
        )
        const val MAX_SPEED = 1500
        val CCCD: UUID = u("00002902-0000-1000-8000-00805f9b34fb")

        fun crc8(b: ByteArray, n: Int): Byte {
            var c = 0
            for (i in 0 until n) {
                c = (c xor b[i].toInt()) and 0xFF
                repeat(8) { c = if (c and 0x80 != 0) ((c shl 1) xor 0x8D) and 0xFF else (c shl 1) and 0xFF }
            }
            return c.toByte()
        }

        fun wrap(type: Int, payload: ByteArray): ByteArray {
            val len = payload.size + 2
            val body = ByteArray(payload.size + 4)
            body[0] = (len shr 8).toByte()
            body[1] = len.toByte()
            body[2] = type.toByte()
            payload.copyInto(body, 3)
            body[body.size - 1] = crc8(body, body.size - 1)
            return byteArrayOf(0xFF.toByte(), 0xFF.toByte()) + body
        }

        private fun le16(v: Int) = byteArrayOf(v.toByte(), (v shr 8).toByte())

        fun handshake() = wrap(1, ByteArray(0))

        fun move(x: Int, y: Int): ByteArray {
            // Hiz siniri: cok yuksek hizla uc noktaya dayaninca gimbal motorlari kendini korumaya alip birakiyor
            val cx = x.coerceIn(-MAX_SPEED, MAX_SPEED)
            // Gimbal'da pozitif dikey hiz asagi bakiyor; uygulamada y>0 = yukari
            val cy = (-y).coerceIn(-MAX_SPEED, MAX_SPEED)
            return wrap(3, byteArrayOf(0xAA.toByte(), 0x55, 0x00, 0x05, 0x20) + le16(cx) + le16(cy))
        }

        fun angle(roll: Int, pitch: Int, yaw: Int) =
            wrap(3, byteArrayOf(0xAA.toByte(), 0x55, 0x00, 0x08, 0x22, 0x02) + le16(roll * 10) + le16(pitch * 10) + le16(yaw * 10))
    }

    private val adapter = ctx.getSystemService(BluetoothManager::class.java).adapter
    private val main = Handler(Looper.getMainLooper())
    private var gatt: BluetoothGatt? = null
    private var writeChar: BluetoothGattCharacteristic? = null
    private val queue = ArrayDeque<ByteArray>()
    private var writing = false
    private var scanning = false
    private var wanted = false
    private val seen = HashSet<String>()

    @Volatile var state = "kapali"
        private set
    @Volatile var deviceName = ""
        private set
    /** Baglantinin kac kez koptugu ve son kopma sebebi (8 = sinyal/zaman asimi, 19 = gimbal kapatti) */
    @Volatile var drops = 0
        private set
    @Volatile var lastDropStatus = -1
        private set
    var onState: ((String) -> Unit)? = null
    /** Gimbal'in gonderdigi durum paketleri (ham). Pil/sarj bilgisini cozmek icin PC'ye aktarilir. */
    var onNotify: ((ByteArray) -> Unit)? = null

    private fun setState(s: String) {
        state = s
        log("Gimbal: $s")
        onState?.invoke(s)
    }

    fun start() {
        wanted = true
        if (adapter == null || !adapter.isEnabled) {
            setState("bluetooth kapali")
            main.postDelayed({ if (wanted && gatt == null) start() }, 3000)
            return
        }
        if (scanning || gatt != null) return
        // Telefonla eslesmis gimbal yayin yapmayabilir: once dogrudan ona baglan
        val bonded = adapter.bondedDevices?.firstOrNull { d -> val n = d.name ?: ""; NAME_PREFIXES.any { n.startsWith(it) } }
        if (bonded != null) {
            connect(bonded, bonded.name ?: bonded.address)
            return
        }
        scanning = true
        setState("araniyor")
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        adapter.bluetoothLeScanner?.startScan(null, settings, scanCb)
        main.postDelayed({
            if (scanning) {
                stopScan()
                setState("bulunamadi, tekrar araniyor")
                if (wanted) start()
            }
        }, 15000)
    }

    fun stop() {
        wanted = false
        stopScan()
        gatt?.close()
        gatt = null
        writeChar = null
        setState("kapali")
    }

    private fun stopScan() {
        if (!scanning) return
        scanning = false
        try { adapter?.bluetoothLeScanner?.stopScan(scanCb) } catch (_: Exception) {}
    }

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val name = result.scanRecord?.deviceName ?: result.device.name ?: return
            if (seen.add(name)) log("BLE gordu: $name")
            if (NAME_PREFIXES.none { name.startsWith(it) }) return
            if (!scanning) return
            stopScan()
            connect(result.device, name)
        }
    }

    private fun connect(d: BluetoothDevice, name: String) {
        deviceName = name
        setState("baglaniyor: $name")
        val g = d.connectGatt(ctx, false, gattCb, BluetoothDevice.TRANSPORT_LE)
        gatt = g
        main.postDelayed({
            if (gatt === g && writeChar == null) {
                g.close()
                gatt = null
                setState("baglanamadi, tekrar deneniyor")
                if (wanted) start()
            }
        }, 12000)
    }

    private val gattCb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            log("BLE durum: status=$status state=$newState")
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                main.post { setState("servisler okunuyor") }
                // Wi-Fi ile ayni 2.4 GHz anteni paylasiyoruz: yuksek oncelik (7.5 ms) Wi-Fi'nin havasini caliyor
                // ve goruntu takiliyor. Gimbal komutlari icin dengeli aralik yeterli.
                g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_BALANCED)
                g.requestMtu(185)
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                if (writeChar != null) { drops++; lastDropStatus = status }
                g.close()
                main.post {
                    gatt = null
                    writeChar = null
                    synchronized(queue) { queue.clear(); writing = false }
                    setState("baglanti koptu (sebep $status)")
                    if (wanted) main.postDelayed({ start() }, 1500)
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            var notifyChar: BluetoothGattCharacteristic? = null
            for ((svc, w, n) in CHANNELS) {
                val s = g.getService(svc) ?: continue
                writeChar = s.getCharacteristic(w) ?: continue
                notifyChar = s.getCharacteristic(n)
                break
            }
            if (writeChar == null) {
                val all = g.services.joinToString { s -> s.uuid.toString().substring(4, 8) + "[" + s.characteristics.joinToString(",") { it.uuid.toString().substring(4, 8) } + "]" }
                main.post { setState("bilinen kanal yok: $all") }
                return
            }
            if (notifyChar != null) {
                g.setCharacteristicNotification(notifyChar, true)
                notifyChar.getDescriptor(CCCD)?.let {
                    if (android.os.Build.VERSION.SDK_INT >= 33) {
                        g.writeDescriptor(it, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
                    } else {
                        @Suppress("DEPRECATION")
                        run { it.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE; g.writeDescriptor(it) }
                    }
                }
            }
            main.postDelayed({
                send(handshake())
                setState("bagli: $deviceName")
            }, 300)
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            synchronized(queue) { writing = false }
            pump()
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            synchronized(queue) { writing = false }
            pump()
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            onNotify?.invoke(value)
        }

        @Deprecated("API 33 oncesi")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            if (android.os.Build.VERSION.SDK_INT < 33) @Suppress("DEPRECATION") c.value?.let { onNotify?.invoke(it.copyOf()) }
        }
    }

    val connected get() = writeChar != null

    fun send(data: ByteArray) {
        if (writeChar == null) return
        synchronized(queue) {
            // Hareket komutlari birikmesin: kuyrukta en fazla birkac paket tut
            while (queue.size > 4) queue.removeFirst()
            queue.addLast(data)
        }
        pump()
    }

    private fun pump() {
        val g = gatt ?: return
        val c = writeChar ?: return
        val next: ByteArray
        synchronized(queue) {
            if (writing || queue.isEmpty()) return
            next = queue.removeFirst()
            writing = true
        }
        val type = if (c.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE != 0)
            BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE else BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        val ok = if (android.os.Build.VERSION.SDK_INT >= 33) {
            g.writeCharacteristic(c, next, type) == BluetoothGatt.GATT_SUCCESS
        } else {
            @Suppress("DEPRECATION")
            run { c.writeType = type; c.value = next; g.writeCharacteristic(c) }
        }
        if (!ok) {
            synchronized(queue) { writing = false }
            main.postDelayed({ pump() }, 20)
        }
    }
}
