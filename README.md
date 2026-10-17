# GimbalCam

Telefon kamerasını PC'ye aktarır (DroidCam benzeri) ve Merak One M1 gimbal'ı PC'den sürer.

- `android/` — telefon uygulaması (Kotlin). Camera2 → donanım H.264 → TCP 8686; kontrol TCP 8687 (satır satır JSON); gimbal'a BLE köprüsü.
- `pc/` — masaüstü uygulaması (PySide6). Görüntü, joystick/klavye, yüz takibi, kamera ayarları, sanal kamera (OBS Virtual Camera), kayıt/fotoğraf.

## Çalıştırma

```
cd android && gradlew assembleDebug && adb install -r -g app/build/outputs/apk/debug/app-debug.apk
cd pc && baslat.bat        # ya da: python app.py
```

USB'de `adb forward` otomatik yapılır; Wi-Fi'de uygulamada "Wi-Fi" seçip telefonun IP'sini yaz.

## Gimbal protokolü (GimbalPlay 2.5.55'ten çıkarıldı)

- Cihaz adı `M1-A`; servis `fff0` (yazma `fff2`, bildirim `fff1`) — `ffe0` ve `22b3a000` de denenir.
- Zarf: `FF FF | uzunluk(BE, payload+2) | tip | payload | crc8` (crc8: uzunluktan itibaren, polinom 0x8D).
- El sıkışma: tip 1, boş payload.
- Hız: tip 3, `AA 55 00 05 20 | yatay int16 LE | dikey int16 LE` (gimbal'da dikey eksen ters; uygulama `y>0 = yukarı` olacak şekilde çevirir).
- Açı: tip 3, `AA 55 00 08 22 02 | roll*10 | pitch*10 | yaw*10`.
- Eşleşmiş gimbal yayın yapmaz; uygulama önce eşleşmiş cihaza doğrudan bağlanır.
