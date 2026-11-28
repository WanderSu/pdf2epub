; pdf2epub Windows 安装器(NSIS 3.x)
;
; 设计约束:
;   - **只装程序,不装数据**:用户数据(设置/书库/work/output/pandoc)在
;     %LOCALAPPDATA%\pdf2epub\,由程序自己管理;卸载**不删**它(见 docs/windows-install.md)。
;   - 默认 per-user 安装($LOCALAPPDATA\Programs\pdf2epub,不弹 UAC);
;     传 /DPER_USER=0 编译成 per-machine 版($PROGRAMFILES64\pdf2epub,需要管理员)。
;   - 升级走同一个安装器:先清掉旧的 cli/ 与 config/(PyInstaller onedir 改名后
;     残留的旧 _internal 文件会和新的混在一起,症状是「升级后引擎起不来」)。
;   - 静默安装:/S,可配 /D=<目录>(/D 必须是最后一个参数且不加引号)。
;
; 编译由 scripts/build_installer.py 负责:它写 installer/_generated.nsh 再调 makensis。

Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"

; ---- 由 build_installer.py 生成的参数(VERSION / SRCDIR / OUTFILE) ----
!include /NONFATAL "_generated.nsh"

!ifndef VERSION
  !error "VERSION 未定义:请用 scripts/build_installer.py 编译,或传 /DVERSION=x.y.z"
!endif
!ifndef SRCDIR
  !error "SRCDIR 未定义:请用 scripts/build_installer.py 编译,或传 /DSRCDIR=<发布目录>"
!endif
!ifndef OUTFILE
  !define OUTFILE "pdf2epub-v${VERSION}-win-x64-setup.exe"
!endif
!ifndef PER_USER
  !define PER_USER 1
!endif

Name "pdf2epub"
OutFile "${OUTFILE}"
SetCompressor /SOLID lzma
SetCompressorDictSize 64
CRCCheck on
XPStyle on
BrandingText "pdf2epub ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "pdf2epub"
VIAddVersionKey /LANG=2052 "FileDescription" "pdf2epub 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "MIT License"
VIAddVersionKey /LANG=1033 "ProductName" "pdf2epub"
VIAddVersionKey /LANG=1033 "FileDescription" "pdf2epub installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "MIT License"

!if ${PER_USER} == 1
  RequestExecutionLevel user
  InstallDir "$LOCALAPPDATA\Programs\pdf2epub"
  !define UNINST_ROOT "Software\Microsoft\Windows\CurrentVersion\Uninstall"
  !define APP_REG_ROOT "Software\pdf2epub"
!else
  RequestExecutionLevel admin
  InstallDir "$PROGRAMFILES64\pdf2epub"
  !define UNINST_ROOT "Software\Microsoft\Windows\CurrentVersion\Uninstall"
  !define APP_REG_ROOT "Software\pdf2epub"
!endif
; 记住用户上次选的安装目录(升级时直接沿用)。
; InstallDirRegKey 只认字面根键(SHCTX 在这里不合法),按模式分开写。
!if ${PER_USER} == 1
  InstallDirRegKey HKCU "${APP_REG_ROOT}" "InstallDir"
!else
  InstallDirRegKey HKLM "${APP_REG_ROOT}" "InstallDir"
!endif

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\pdf2epub.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 pdf2epub"
!define MUI_FINISHPAGE_LINK "使用说明(README)"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\README.md"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ; per-user 版不弹 UAC;per-machine 版由 RequestExecutionLevel admin 处理
  !if ${PER_USER} == 1
    SetShellVarContext current
  !else
    SetShellVarContext all
    ${IfNot} ${RunningX64}
      MessageBox MB_ICONSTOP "需要 64 位 Windows。"
      Abort
    ${EndIf}
  !endif
FunctionEnd

Section "pdf2epub(必需)" SEC_MAIN
  SectionIn RO
  !if ${PER_USER} == 1
    SetShellVarContext current
  !else
    SetShellVarContext all
  !endif

  ; 升级:先清旧引擎与配置,避免 onedir 残留
  RMDir /r "$INSTDIR\cli"
  RMDir /r "$INSTDIR\config"

  SetOutPath "$INSTDIR"
  File "${SRCDIR}\pdf2epub.exe"
  File "${SRCDIR}\README.md"

  ; File /r 会按**通配符所在目录**为根展开,所以必须先把 OutPath 切到目标子目录,
  ; 否则 cli\ 里的文件会平铺到安装根(实测:cli.exe 与 _internal/ 落在 $INSTDIR)。
  SetOutPath "$INSTDIR\cli"
  File /r "${SRCDIR}\cli\*.*"

  SetOutPath "$INSTDIR\config"
  File /r "${SRCDIR}\config\*.*"

  ; 可选:发布目录里带了内置 pandoc 就一起装上
  !if /FileExists "${SRCDIR}\pandoc\pandoc.exe"
    RMDir /r "$INSTDIR\pandoc"
    SetOutPath "$INSTDIR\pandoc"
    File /r "${SRCDIR}\pandoc\*.*"
  !endif

  SetOutPath "$INSTDIR"

  ; 开始菜单(不建桌面快捷方式:静默安装不该往桌面丢图标)
  CreateDirectory "$SMPROGRAMS\pdf2epub"
  CreateShortcut "$SMPROGRAMS\pdf2epub\pdf2epub.lnk" "$INSTDIR\pdf2epub.exe"
  CreateShortcut "$SMPROGRAMS\pdf2epub\卸载 pdf2epub.lnk" "$INSTDIR\Uninstall.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; 安装目录记到注册表,升级时沿用
  WriteRegStr SHCTX "${APP_REG_ROOT}" "InstallDir" "$INSTDIR"
  WriteRegStr SHCTX "${APP_REG_ROOT}" "Version" "${VERSION}"

  ; 控制面板「程序和功能」
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "DisplayName" "pdf2epub"
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "DisplayVersion" "${VERSION}"
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "Publisher" "WanderSu"
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "URLInfoAbout" "https://github.com/WanderSu/pdf2epub"
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "DisplayIcon" "$INSTDIR\pdf2epub.exe"
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "InstallLocation" "$INSTDIR"
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr SHCTX "${UNINST_ROOT}\pdf2epub" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD SHCTX "${UNINST_ROOT}\pdf2epub" "NoModify" 1
  WriteRegDWORD SHCTX "${UNINST_ROOT}\pdf2epub" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD SHCTX "${UNINST_ROOT}\pdf2epub" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  !if ${PER_USER} == 1
    SetShellVarContext current
  !else
    SetShellVarContext all
  !endif

  Delete "$SMPROGRAMS\pdf2epub\pdf2epub.lnk"
  Delete "$SMPROGRAMS\pdf2epub\卸载 pdf2epub.lnk"
  RMDir "$SMPROGRAMS\pdf2epub"

  ; 只删安装目录里的程序文件。用户数据在 %LOCALAPPDATA%\pdf2epub\,
  ; **不在这里**,不能顺手 RMDir —— 那会连书库和已计费的解析缓存一起删掉。
  RMDir /r "$INSTDIR\cli"
  RMDir /r "$INSTDIR\config"
  RMDir /r "$INSTDIR\pandoc"
  Delete "$INSTDIR\pdf2epub.exe"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey SHCTX "${UNINST_ROOT}\pdf2epub"
  DeleteRegKey SHCTX "${APP_REG_ROOT}"
SectionEnd
