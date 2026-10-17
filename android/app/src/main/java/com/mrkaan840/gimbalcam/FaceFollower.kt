package com.mrkaan840.gimbalcam

import android.graphics.RectF
import kotlin.math.abs

/**
 * Telefonda yuz takibi. Kameranin donanimsal yuz algilamasindan gelen kutulari alir ve gimbal hizini
 * hesaplar. Ag dongunun disinda oldugu icin gecikme/salinim yok.
 *
 * Koordinatlar: kutular ham goruntude 0..1 (x saga, y asagi). PC'nin gosterdigi "dik" goruntuye
 * [rot] kadar saat yonunde dondurulur; hata o goruntude hesaplanir, eksen eslemesi (PC'nin olctugu)
 * ile gimbal komutuna cevrilir.
 */
class FaceFollower {
    // PC'den gelen ayarlar
    @Volatile var enabled = false
    @Volatile var rot = 0                 // 0/90/180/270, PC'nin gosterdigi dondurme
    @Volatile var hAxis = "x"; @Volatile var hSign = 1   // goruntude saga = hangi motor, isaret
    @Volatile var vAxis = "y"; @Volatile var vSign = 1   // goruntude yukari
    @Volatile var speed = 800
    @Volatile var dead = 0.08

    // Durum
    var box: RectF? = null                // dik goruntude 0..1
        private set
    var searching = false
        private set
    private var lost = 0
    private var lastE: Pair<Double, Double>? = null
    private var lastSeen = 0L
    private val pe = doubleArrayOf(0.0, 0.0)
    private val de = doubleArrayOf(0.0, 0.0)
    private val active = booleanArrayOf(false, false)
    private var t = System.currentTimeMillis()

    private fun rotPoint(x: Float, y: Float): Pair<Float, Float> = when (rot) {
        90 -> Pair(1 - y, x)
        180 -> Pair(1 - x, 1 - y)
        270 -> Pair(y, 1 - x)
        else -> Pair(x, y)
    }

    private fun toUpright(r: RectF): RectF {
        val a = rotPoint(r.left, r.top)
        val b = rotPoint(r.right, r.bottom)
        return RectF(minOf(a.first, b.first), minOf(a.second, b.second), maxOf(a.first, b.first), maxOf(a.second, b.second))
    }

    /** Her kamera sonucunda cagrilir. Donus: gimbal hiz komutu (x, y) ya da null (takip kapali). */
    fun update(rawFaces: List<RectF>): Pair<Int, Int>? {
        if (!enabled) { box = null; searching = false; resetSearch(); return null }
        val now = System.currentTimeMillis()
        val faces = rawFaces.map { toUpright(it) }
        if (faces.isNotEmpty()) {
            val prev = box
            box = if (prev != null) faces.minBy { (it.centerX() - prev.centerX()).let { d -> d * d } + (it.centerY() - prev.centerY()).let { d -> d * d } }
            else faces.maxBy { it.width() * it.height() }
            lost = 0
        } else if (++lost > 8) {
            box = null
        }
        val b = box ?: return toMotor(search(now))
        searching = false
        resetSearch()
        val ex = (b.centerX() - 0.5) / 0.5                       // + = yuz sagda
        val ey = (0.5 - (b.top + b.height() / 2.5)) / 0.5        // + = yuz yukarida (yuzu biraz uste koy)
        lastE = Pair(ex, ey)
        lastSeen = now
        val dt = ((now - t) / 1000.0).coerceIn(0.01, 0.2)
        t = now
        val out = DoubleArray(2)
        for ((i, e) in doubleArrayOf(ex, ey).withIndex()) {
            // Histerezisli olu bolge
            if (active[i] && abs(e) < dead) active[i] = false
            else if (!active[i] && abs(e) > dead * 1.8) active[i] = true
            // Turev: merkeze hizla yaklasirken fren (asmayi onler)
            de[i] = (0.6 * de[i] + 0.4 * ((e - pe[i]) / dt)).coerceIn(-5.0, 5.0)
            pe[i] = e
            lastV[i] = de[i]
            if (!active[i]) { out[i] = 0.0; continue }
            var u = 0.75 * e + 0.12 * de[i]
            if (u * e < 0) u = 0.0
            out[i] = u.coerceIn(-0.6, 0.6)
        }
        return toMotor(Pair(out[0], out[1]))
    }

    // ---- Arama ----
    // Yuz kaybolunca sirayla: 1) son goruldugu yone, 2) ters yone (baslangici gecip diger tarafi da tara),
    // 3) yukari-asagi tarama, 4) bulamazsa son gordugu aciya geri don ve bekle.
    // Gimbal mutlak aci bildirmiyor: arama boyunca verilen hareketler toplanir (netX/netY, "hiz x saniye"),
    // geri donuste tersine uygulanir.
    private var phase = 0          // 0 yok, 1 yone, 2 ters, 3 dikey, 4 geri donus, 5 bitti
    private val lastV = doubleArrayOf(0.0, 0.0)   // yuzun son hareket hizi (kayboldugu yon)
    private var vDir = 0.0
    private var phaseAt = 0L
    private var dir = 1.0
    private var netX = 0.0
    private var netY = 0.0
    private var lastSearchAt = 0L

    private fun resetSearch() { phase = 0; netX = 0.0; netY = 0.0 }

    /** Elle kontrol / uyku: arama hareketleri uygulanmadi, geri donus hesabi bozulur -> aramayi birak. */
    fun cancelSearch() { if (phase in 1..4) phase = 5 }

    private fun search(now: Long): Pair<Double, Double> {
        val e = lastE
        if (e == null) { searching = false; return Pair(0.0, 0.0) }
        if (phase == 0) {
            phase = 1; phaseAt = now; netX = 0.0; netY = 0.0; lastSearchAt = now
            // Son goruldugu yon: konum + hareket yonu (kenara varmadan kaybolduysa hareket belirler)
            val hx = e.first + 0.35 * lastV[0]
            val hy = e.second + 0.35 * lastV[1]
            dir = if (hx >= 0) 1.0 else -1.0
            vDir = if (abs(hy) > 0.3) (if (hy > 0) 1.0 else -1.0) else 0.0
        }
        val dt = ((now - lastSearchAt) / 1000.0).coerceIn(0.0, 0.2)
        lastSearchAt = now
        val inPhase = (now - phaseAt) / 1000.0
        fun next(p: Int) { phase = p; phaseAt = now }
        val H = 0.45; val V = 0.3
        var sx = 0.0; var sy = 0.0
        when (phase) {
            1 -> if (inPhase < 3.0) {
                sx = H * dir
                // Yukari/asagi ciktiysa ilk 1 sn o yone de bak (uca dayanmasin diye kisa)
                if (inPhase < 1.0) sy = V * vDir
            } else next(2)
            2 -> if (inPhase < 6.0) sx = -H * dir else next(3)
            3 -> when {
                inPhase < 1.5 -> sy = V          // yukari
                inPhase < 4.5 -> sy = -V         // asagi (ortayi gecip)
                inPhase < 6.0 -> sy = V          // ortaya
                else -> next(4)
            }
            4 -> {
                // Son gordugu aciya don: toplanan hareketi tersine uygula
                if (abs(netX) > 0.05) sx = if (netX > 0) -H else H
                if (abs(netY) > 0.05) sy = if (netY > 0) -V else V
                if (sx == 0.0 && sy == 0.0) next(5)
            }
        }
        // Geri donuste hedefi asmamak icin son adimi kirp
        if (phase == 4) {
            if (sx != 0.0 && abs(sx * dt) > abs(netX)) sx = -netX / maxOf(dt, 0.01)
            if (sy != 0.0 && abs(sy * dt) > abs(netY)) sy = -netY / maxOf(dt, 0.01)
        }
        netX += sx * dt
        netY += sy * dt
        searching = phase in 1..4
        return Pair(sx, sy)
    }

    /** Dik goruntudeki (saga, yukari) istegini gimbal motor komutuna cevir. */
    private fun toMotor(v: Pair<Double, Double>): Pair<Int, Int> {
        var x = 0.0; var y = 0.0
        val r = v.first * hSign * speed; val u = v.second * vSign * speed
        if (hAxis == "x") x += r else y += r
        if (vAxis == "x") x += u else y += u
        return Pair(x.toInt(), y.toInt())
    }
}
