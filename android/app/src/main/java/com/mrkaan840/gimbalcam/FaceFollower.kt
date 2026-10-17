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
        if (!enabled) { box = null; searching = false; return null }
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
            de[i] = 0.6 * de[i] + 0.4 * ((e - pe[i]) / dt)
            pe[i] = e
            if (!active[i]) { out[i] = 0.0; continue }
            var u = 0.75 * e + 0.12 * de[i]
            if (u * e < 0) u = 0.0
            out[i] = u.coerceIn(-0.6, 0.6)
        }
        return toMotor(Pair(out[0], out[1]))
    }

    /** Yuz kadrajdan cikti: son goruldugu tarafa kisa sure don (dikey kisa; uca dayanmasin). */
    private fun search(now: Long): Pair<Double, Double> {
        searching = false
        val e = lastE ?: return Pair(0.0, 0.0)
        val gone = (now - lastSeen) / 1000.0
        var sx = 0.0; var sy = 0.0
        if (gone < 3.0 && abs(e.first) > 0.2) sx = if (e.first > 0) 0.45 else -0.45
        if (gone < 1.0 && abs(e.second) > 0.3) sy = if (e.second > 0) 0.3 else -0.3
        searching = sx != 0.0 || sy != 0.0
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
