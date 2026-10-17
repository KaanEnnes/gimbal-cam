; GimbalCam kurulum dosyasi (NSIS). Derleme: build_release.ps1
Unicode true
SetCompressor /SOLID lzma

!define APPNAME "GimbalCam"
!ifndef VERSION
  !define VERSION "1.0.0"
!endif

Name "${APPNAME}"
OutFile "dist\GimbalCam-Kurulum.exe"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "Software\kaanai\${APPNAME}" "InstallDir"
RequestExecutionLevel user
BrandingText "kaanai.site"

!include "MUI2.nsh"
!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\GimbalCam.exe"
!define MUI_FINISHPAGE_RUN_TEXT "GimbalCam'i başlat"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Turkish"

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=${LANG_TURKISH} "ProductName" "${APPNAME}"
VIAddVersionKey /LANG=${LANG_TURKISH} "FileDescription" "${APPNAME} kurulumu"
VIAddVersionKey /LANG=${LANG_TURKISH} "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=${LANG_TURKISH} "LegalCopyright" "kaanai.site"

Section "Kur"
  ; Eski surum aciksa kapat
  nsExec::Exec 'taskkill /F /IM GimbalCam.exe'
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\_internal"
  File /r "dist\GimbalCam\*.*"
  WriteUninstaller "$INSTDIR\Kaldir.exe"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\GimbalCam.exe"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME} kaldır.lnk" "$INSTDIR\Kaldir.exe"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\GimbalCam.exe"
  WriteRegStr HKCU "Software\kaanai\${APPNAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "kaanai.site"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayIcon" "$INSTDIR\GimbalCam.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$\"$INSTDIR\Kaldir.exe$\""
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  nsExec::Exec 'taskkill /F /IM GimbalCam.exe'
  nsExec::Exec '"$INSTDIR\_internal\adb\adb.exe" kill-server'
  RMDir /r "$INSTDIR"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
  DeleteRegKey HKCU "Software\kaanai\${APPNAME}"
SectionEnd
