# GimbalCam surum derleme: telefon APK'si + PC exe + kurulum dosyasi
#   powershell -ExecutionPolicy Bypass -File build_release.ps1 -Version 1.0.0
param([string]$Version = "1.0.0")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$sdk = "$env:LOCALAPPDATA\Android\Sdk"
$out = Join-Path $root "release"
New-Item -ItemType Directory -Force $out | Out-Null

# 1) Telefon uygulamasi
Push-Location (Join-Path $root "android")
.\gradlew.bat assembleRelease --console=plain -q
Copy-Item app\build\outputs\apk\release\app-release.apk (Join-Path $out "GimbalCam.apk") -Force
Pop-Location

# 2) PC uygulamasi
Push-Location (Join-Path $root "pc")
New-Item -ItemType Directory -Force adb | Out-Null
Copy-Item "$sdk\platform-tools\adb.exe", "$sdk\platform-tools\AdbWinApi.dll", "$sdk\platform-tools\AdbWinUsbApi.dll" adb -Force
python -m PyInstaller --noconfirm --clean --onedir --windowed --name GimbalCam --icon icon.ico `
  --add-data "face_detection_yunet_2023mar.onnx;." --add-data "icon.png;." `
  --add-binary "adb/adb.exe;adb" --add-binary "adb/AdbWinApi.dll;adb" --add-binary "adb/AdbWinUsbApi.dll;adb" `
  --hidden-import kablosuz_eslestir --exclude-module tkinter --exclude-module matplotlib app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller basarisiz" }
# Kullanilmayan Qt parcalari (QML/Quick/PDF/yazilim OpenGL/ceviriler) -> boyut kucultme
$qt = "dist\GimbalCam\_internal\PySide6"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "$qt\translations", "$qt\opengl32sw.dll", "$qt\Qt6Quick*.dll", "$qt\Qt6Qml*.dll", "$qt\Qt6Pdf*.dll", "$qt\Qt6VirtualKeyboard*.dll", "$qt\qml"

# 3) Kurulum dosyasi
$nsis = Get-ChildItem "$env:LOCALAPPDATA\electron-builder\Cache\nsis" -Recurse -Filter makensis.exe | Select-Object -First 1
& $nsis.FullName /V2 "/DVERSION=$Version" installer.nsi
if ($LASTEXITCODE -ne 0) { throw "NSIS basarisiz" }
Copy-Item dist\GimbalCam-Kurulum.exe (Join-Path $out "GimbalCam-Kurulum.exe") -Force
Pop-Location

Get-ChildItem $out | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
