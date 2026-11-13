; ─────────────────────────────────────────────────────────────────────────────
;  Copper for KiCad — Windows installer (NSIS 3, Modern UI 2)
;
;  Page flow mirrors KiCad's own installer:
;    Welcome → License → Components → Install location → Install → Finish
;
;  The Finish page opens Copper setup: a local page in the user's browser
;  (`copperw.exe setup --web`, no console window) that shows the KiCad that was
;  found, asks what to bring along from it, connects AI apps and runs a test call.
;
;  Per-user, no admin: installs to %LOCALAPPDATA%\Programs\Copper.
;  Bundles uv.exe, a standalone CPython 3.12, the copper wheel and Pillow wheels,
;  so it works offline; runs `uv tool install` into the install dir, then `copper install --client …`
;  for each AI app the user ticked.
;
;  Built by scripts/build_release.py, which passes:
;    /DVERSION=1.1.1 /DSTAGE=<staging dir> /DWHEEL=<wheel file name> /DOUTFILE=<exe>
;
;  Test mode: if the environment variable COPPER_CONFIG_HOME_OVERRIDE is set
;  (or /NOSYSTEM is passed) the installer skips everything outside the install
;  dir (registry, PATH, Start menu) and client detection uses that fake home.
; ─────────────────────────────────────────────────────────────────────────────

Unicode true
ManifestDPIAware true
SetCompressor /SOLID lzma

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef STAGE
  !error "pass /DSTAGE=<staging dir>"
!endif
!ifndef WHEEL
  !error "pass /DWHEEL=<wheel file name>"
!endif
!ifndef PYVER
  !define PYVER "3.12"
!endif
!ifndef OUTFILE
  !define OUTFILE "Copper-for-KiCad-${VERSION}-Setup.exe"
!endif

!define PRODUCT      "Copper for KiCad"
!define PUBLISHER    "Clay Nicholson"
!define WEBSITE      "https://coppereda.com"
!define UNINST_KEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\CopperForKiCad"
!define APP_KEY      "Software\Copper for KiCad"

Name "${PRODUCT} ${VERSION}"
Caption "${PRODUCT} ${VERSION} Setup"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Copper"
InstallDirRegKey HKCU "${APP_KEY}" "InstallDir"
RequestExecutionLevel user
BrandingText "${PRODUCT} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${PRODUCT}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "${PRODUCT} Setup"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "MIT License"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"
!include "WinMessages.nsh"

Var Clients      ; comma list of client ids to register
Var NoSystem     ; 1 = test mode: touch nothing outside $INSTDIR
Var NoKiCadCfg   ; 1 = test mode without KICAD_CONFIG_HOME: never touch the real KiCad config
Var HomeDir      ; %USERPROFILE% (or override)
Var RoamDir      ; %APPDATA%      (or override)
Var LocalDir     ; %LOCALAPPDATA% (or override)
Var KiCadPython  ; KiCad's bundled python.exe, used as an offline fallback
Var KiCadFound

; ── look & feel ────────────────────────────────────────────────────────────
!define MUI_ICON   "assets\copper.ico"
!define MUI_UNICON "assets\copper.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP   "assets\welcome.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "assets\welcome.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP   "assets\header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "assets\header.bmp"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_SMALLDESC

!define MUI_WELCOMEPAGE_TITLE "Welcome to the ${PRODUCT} ${VERSION} Setup"
!define MUI_WELCOMEPAGE_TITLE_3LINES
!define MUI_WELCOMEPAGE_TEXT "Copper lets the AI apps you already use (Claude, Codex, Cursor and others) design real circuit boards in KiCad: schematics, LCSC/JLCPCB parts, layout, routing and fabrication files. KiCad checks every change.$\r$\n$\r$\nSetup takes about a minute and needs no administrator rights. It installs for you only, in its own folder, and does not change your KiCad installation.$\r$\n$\r$\nClick Next to continue."

!define MUI_LICENSEPAGE_TEXT_TOP "Copper for KiCad is free, open-source software (MIT License)."

!define MUI_COMPONENTSPAGE_TEXT_TOP "Pick the AI apps Copper should connect to. Apps found on this PC are ticked; you can connect more later from Copper setup."

!define MUI_DIRECTORYPAGE_TEXT_TOP "Copper installs into this folder, with its own private Python. The default needs no administrator rights, and uninstalling removes the whole folder."

!define MUI_FINISHPAGE_TITLE "${PRODUCT} is ready"
!define MUI_FINISHPAGE_TEXT "Copper is installed and connected to the AI apps you picked.$\r$\n$\r$\nNext, Copper setup opens in your browser. It brings your KiCad libraries, theme and hotkeys along (only what you tick) and makes one real test call.$\r$\n$\r$\nThen restart your AI app (quit Claude Desktop from the tray icon) and ask:$\r$\n“Use Copper to design a USB-C powered ESP32-C3 board with a status LED.”$\r$\n$\r$\nAdded an AI app later? Start menu › Copper setup, or run: copper install --client cursor"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open Copper setup in my browser (recommended)"
!define MUI_FINISHPAGE_RUN_FUNCTION RunSetup
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\QUICKSTART.html"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show the quick-start guide"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!define MUI_FINISHPAGE_LINK "Boards made with Copper: coppereda.com"
!define MUI_FINISHPAGE_LINK_LOCATION "${WEBSITE}"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}\LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ── helpers ────────────────────────────────────────────────────────────────
!macro SetEnv NAME VALUE
  System::Call 'Kernel32::SetEnvironmentVariable(t "${NAME}", t "${VALUE}")i'
!macroend

!macro AddClient ID
  StrCpy $Clients "$Clients,${ID}"
!macroend

; copperw.exe is the windowless launcher (PE subsystem GUI): no console flashes up.
; It serves the setup page on 127.0.0.1 and opens the default browser on it.
Function RunSetup
  ${If} ${FileExists} "$INSTDIR\bin\copperw.exe"
    Exec '"$INSTDIR\bin\copperw.exe" setup --web'
  ${Else}
    ExecShell "open" "$INSTDIR\copper-doctor.cmd"
  ${EndIf}
FunctionEnd

; AI apps (Claude Desktop in the tray, Cursor, Codex …) keep Copper's MCP server
; running from $INSTDIR. Its open files lock the environment, and `uv tool install
; --force` / RMDir then leave it half-deleted (copper.exe present, package gone).
; Stop every process whose image lives under $INSTDIR first — with consent.
!macro DEFINE_STOP_COPPER UN
Function ${UN}StopRunningCopper
  ${IfNot} ${FileExists} "$INSTDIR\tools\*.*"
    Return
  ${EndIf}
  System::Call 'Kernel32::SetEnvironmentVariable(t "COPPER_DIR", t "$INSTDIR")i'
  ; Setup is a 32-bit process: plain "powershell.exe" would be the WOW64 one, which
  ; cannot read the image path of 64-bit processes (Path is empty) and finds nothing.
  StrCpy $R9 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  ${IfNot} ${FileExists} "$R9"
    StrCpy $R9 "powershell.exe"
  ${EndIf}
  StrCpy $R8 `"$R9" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$d=(Get-Item -LiteralPath $$env:COPPER_DIR).FullName.TrimEnd('\')+'\'; $$p=@(Get-Process | Where-Object { $$_.Path -and $$_.Path.StartsWith($$d, [StringComparison]::OrdinalIgnoreCase) });`
  ; the count comes back as the exit code (nsExec's captured stdout is not reliable here)
  nsExec::ExecToStack `$R8 exit [Math]::Min($$p.Count, 200)"`
  Pop $1
  Pop $0
  ${If} $1 == "error"
  ${OrIf} $1 == "timeout"
    Return            ; no PowerShell: nothing we can do, the version check below reports failure
  ${EndIf}
  IntOp $1 $1 + 0
  ${If} $1 > 0
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "Copper is running in $1 process(es) started by your AI apps (Claude Desktop, Cursor, Codex …), which locks its files.$\r$\n$\r$\nClick OK to stop them now (the apps reconnect to Copper when you restart them), or Cancel to quit those apps yourself and run Setup again." /SD IDOK IDOK +2
      Abort "Close your AI apps, then run Setup again."
    DetailPrint "Stopping $1 running Copper process(es)…"
    nsExec::ExecToLog `$R8 $$p | Stop-Process -Force"`
    Pop $0
    Sleep 1500
  ${EndIf}
FunctionEnd
!macroend
!insertmacro DEFINE_STOP_COPPER ""
!insertmacro DEFINE_STOP_COPPER "un."

; ── sections ───────────────────────────────────────────────────────────────
Section "Copper core (required)" SecCore
  SectionIn RO
  Call StopRunningCopper
  SetOutPath "$INSTDIR"
  File "${STAGE}\uv.exe"
  File "${STAGE}\LICENSE.txt"
  File "${STAGE}\QUICKSTART.html"
  File "${STAGE}\copper-doctor.cmd"
  File "assets\copper.ico"
  SetOutPath "$INSTDIR\wheels"
  File "${STAGE}\wheels\*.whl"
  SetOutPath "$INSTDIR"

  ; Keep uv's tool venv, launcher and managed Python inside the install dir so
  ; uninstalling removes everything; cache in the temp plugins dir.
  InitPluginsDir
  !insertmacro SetEnv "UV_TOOL_DIR"           "$INSTDIR\tools"
  !insertmacro SetEnv "UV_TOOL_BIN_DIR"       "$INSTDIR\bin"
  !insertmacro SetEnv "UV_PYTHON_INSTALL_DIR" "$INSTDIR\python"
  !insertmacro SetEnv "UV_CACHE_DIR"          "$PLUGINSDIR\uv-cache"
  !insertmacro SetEnv "UV_LINK_MODE"          "copy"
  !insertmacro SetEnv "UV_NO_PROGRESS"        "1"
  !insertmacro SetEnv "PYTHONUTF8"            "1"

  ; Bundled standalone CPython (python-build-standalone) served to uv as a
  ; local download mirror, so the whole install works offline.
  SetOutPath "$INSTDIR\python-dist"
  File /r "${STAGE}\python-dist\*.*"
  SetOutPath "$INSTDIR"
  !insertmacro SetEnv "UV_PYTHON_INSTALL_MIRROR" "file:///$INSTDIR\python-dist"

  DetailPrint "Installing a private Python ${PYVER} for Copper…"
  nsExec::ExecToLog '"$INSTDIR\uv.exe" python install ${PYVER} --offline --no-bin --no-registry'
  Pop $0

  DetailPrint "Installing Copper ${VERSION} into its own environment…"
  ; 1) fully offline: bundled Python + bundled wheels
  nsExec::ExecToLog '"$INSTDIR\uv.exe" tool install --force --offline --managed-python --python ${PYVER} --find-links "$INSTDIR\wheels" "$INSTDIR\wheels\${WHEEL}"'
  Pop $0
  ${If} $0 != 0
    ; 2) online: let uv resolve/download whatever is missing
    DetailPrint "Offline install failed — retrying with network access…"
    nsExec::ExecToLog '"$INSTDIR\uv.exe" tool install --force --python 3.12 --find-links "$INSTDIR\wheels" "$INSTDIR\wheels\${WHEEL}"'
    Pop $0
  ${EndIf}
  ${If} $0 != 0
  ${AndIf} $KiCadPython != ""
    ; 3) offline fallback: build the environment on KiCad's own Python
    DetailPrint "Falling back to KiCad's bundled Python ($KiCadPython)…"
    nsExec::ExecToLog '"$INSTDIR\uv.exe" tool install --force --offline --python "$KiCadPython" --find-links "$INSTDIR\wheels" "$INSTDIR\wheels\${WHEEL}"'
    Pop $0
  ${EndIf}
  ; The launcher existing proves nothing (an interrupted upgrade leaves it behind
  ; with the package gone): require that Copper actually starts.
  StrCpy $1 "1"
  ${If} ${FileExists} "$INSTDIR\bin\copper.exe"
    nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" version'
    Pop $1
  ${EndIf}
  ${If} $1 != 0
    DetailPrint "Copper's environment is not usable (uv exit $0, copper version exit $1)."
    MessageBox MB_ICONSTOP|MB_OK "Copper could not create its Python environment (uv exit code $0).$\r$\n$\r$\nQuit your AI apps (Claude Desktop, Cursor, …), check your internet connection and run Setup again. Details are in the log above." /SD IDOK
    Abort "Copper core installation failed."
  ${EndIf}
  RMDir /r "$INSTDIR\python-dist"

  ; --yes changes nothing in KiCad's own settings (library tables, themes, hotkeys, the IPC API):
  ; those are offered item by item on the setup page the Finish page opens.
  ${If} $NoKiCadCfg != "1"
    DetailPrint "Looking at your KiCad setup (nothing in KiCad is changed here)…"
    nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" setup --yes --clients none --api skip --no-doctor --quiet'
    Pop $0
  ${EndIf}

  ; ── system integration (skipped in test mode) ──
  ${If} $NoSystem != "1"
    WriteRegStr HKCU "${APP_KEY}" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "${APP_KEY}" "Version" "${VERSION}"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${PRODUCT}"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
    WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
    WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "${WEBSITE}"
    WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\copper.ico"
    WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
    WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
    WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

    CreateDirectory "$SMPROGRAMS\Copper for KiCad"
    ${If} ${FileExists} "$INSTDIR\bin\copperw.exe"
      CreateShortCut "$SMPROGRAMS\Copper for KiCad\Copper setup.lnk" "$INSTDIR\bin\copperw.exe" "setup --web" "$INSTDIR\copper.ico"
    ${EndIf}
    CreateShortCut "$SMPROGRAMS\Copper for KiCad\Copper doctor.lnk" "$INSTDIR\copper-doctor.cmd" "" "$INSTDIR\copper.ico"
    CreateShortCut "$SMPROGRAMS\Copper for KiCad\Copper quick start.lnk" "$INSTDIR\QUICKSTART.html" "" "$INSTDIR\copper.ico"
    CreateShortCut "$SMPROGRAMS\Copper for KiCad\Uninstall Copper.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\copper.ico"

    DetailPrint "Adding Copper to your PATH (new terminals can run `copper`)…"
    nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" _winpath add "$INSTDIR\bin"'
    Pop $0
  ${Else}
    FileOpen $1 "$INSTDIR\nosystem.flag" w
    FileWrite $1 "test-mode install: no registry/PATH/Start menu changes"
    FileClose $1
  ${EndIf}

  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

SectionGroup /e "Connect to AI apps" SecGroupClients
  Section "Claude Desktop" SecClaudeDesktop
    !insertmacro AddClient "claude-desktop"
  SectionEnd
  Section "Claude Code" SecClaudeCode
    !insertmacro AddClient "claude-code"
  SectionEnd
  Section "OpenAI Codex" SecCodex
    !insertmacro AddClient "codex"
  SectionEnd
  Section "Cursor" SecCursor
    !insertmacro AddClient "cursor"
  SectionEnd
  Section "VS Code (GitHub Copilot)" SecVSCode
    !insertmacro AddClient "vscode"
  SectionEnd
  Section "Windsurf / Devin" SecWindsurf
    !insertmacro AddClient "windsurf"
  SectionEnd
  Section "Zed" SecZed
    !insertmacro AddClient "zed"
  SectionEnd
  Section "Gemini CLI" SecGemini
    !insertmacro AddClient "gemini"
  SectionEnd
  Section "GitHub Copilot CLI" SecCopilotCli
    !insertmacro AddClient "copilot-cli"
  SectionEnd
SectionGroupEnd

Section "Enable KiCad scripting API" SecApi
  ${If} $NoKiCadCfg == "1"
    DetailPrint "Test mode: KiCad config left alone (set KICAD_CONFIG_HOME to test this step)."
    Return
  ${EndIf}
  DetailPrint "Enabling KiCad's IPC API (lets Copper edit an open board)…"
  nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" kicad-api enable'
  Pop $0
  ${If} $0 != 0
    DetailPrint "KiCad is running — close it and run: copper kicad-api enable"
  ${EndIf}
SectionEnd

; Ticked by default: the advertised loop (pcb_place → pcb_route → pcb_pour → fab_export)
; needs it. Unticked in test mode unless COPPER_HOME points the download at a sandbox.
Section "Autorouter: Freerouting + Java (about 120 MB download)" SecFreerouting
  DetailPrint "Downloading Freerouting and a Java runtime…"
  nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" freerouting --install'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Freerouting download failed — retry later with: copper freerouting --install"
  ${EndIf}
SectionEnd

Section "-Register clients" SecRegister
  ${If} $Clients != ""
    StrCpy $Clients $Clients "" 1
    DetailPrint "Registering Copper with: $Clients"
    nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" install --client $Clients'
    Pop $0
    ${If} $0 != 0
      DetailPrint "Some apps could not be registered — run: copper doctor"
    ${EndIf}
  ${EndIf}
SectionEnd

; ── component descriptions (shown under the list, like KiCad's installer) ──
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}         "The Copper MCP server and the copper command, in a private Python environment inside the install folder. Nothing else on your PC changes."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecGroupClients} "Adds Copper to the AI apps found on this PC (ticked). Each settings file is backed up first (*.copper-backup). An app marked (not found) gets its file prepared for later."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecClaudeDesktop} "Adds Copper to claude_desktop_config.json (also the Microsoft Store copy)."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecClaudeCode}   "Adds Copper as a user-scope MCP server for Claude Code."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCodex}        "Adds [mcp_servers.copper] to ~/.codex/config.toml with long timeouts for routing and exports."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCursor}       "Adds Copper to ~/.cursor/mcp.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecVSCode}       "Adds Copper to your VS Code user mcp.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecWindsurf}     "Adds Copper to Windsurf's mcp_config.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecZed}          "Adds Copper to Zed's context_servers."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecGemini}       "Adds Copper to ~/.gemini/settings.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCopilotCli}   "Adds Copper to ~/.copilot/mcp-config.json."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecApi}          "Turns on KiCad's IPC API (Preferences > Plugins) so Copper can edit a board while it is open in KiCad. Close KiCad first; Copper setup can also do this later."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecFreerouting}  "Downloads the Freerouting autorouter and a Java runtime (about 120 MB, once) into %USERPROFILE%\.copper\tools so your AI can autoroute boards. Untick it and the first autoroute downloads it instead (or run: copper freerouting --install)."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ── detection ──────────────────────────────────────────────────────────────
; Detected apps are pre-ticked. Apps not found stay listed (unticked, "not found"),
; so someone who installs Copper first can still connect an app installed later:
; `copper install --client <id>` creates the app's config file.
!macro ShowIf SEC DETECTED NAME
  ${If} ${DETECTED} == "1"
    !insertmacro SelectSection ${SEC}
  ${Else}
    !insertmacro UnselectSection ${SEC}
    SectionSetText ${SEC} "${NAME} (not found)"
  ${EndIf}
!macroend

!macro DetectPath VAR PATH
  StrCpy ${VAR} "0"
  IfFileExists "${PATH}" 0 +2
    StrCpy ${VAR} "1"
!macroend

Function .onInit
  StrCpy $Clients ""
  StrCpy $NoSystem "0"
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/NOSYSTEM" $R1
  ${IfNot} ${Errors}
    StrCpy $NoSystem "1"
  ${EndIf}

  StrCpy $NoKiCadCfg "0"
  ReadEnvStr $R2 "COPPER_CONFIG_HOME_OVERRIDE"
  ${If} $R2 != ""
    StrCpy $NoSystem "1"
    StrCpy $HomeDir  "$R2"
    StrCpy $RoamDir  "$R2\AppData\Roaming"
    StrCpy $LocalDir "$R2\AppData\Local"
  ${Else}
    StrCpy $HomeDir  "$PROFILE"
    StrCpy $RoamDir  "$APPDATA"
    StrCpy $LocalDir "$LOCALAPPDATA"
  ${EndIf}

  ; Test mode must not edit the user's real KiCad config (library tables, IPC API)
  ; unless the test also points KiCad at a sandbox via KICAD_CONFIG_HOME.
  ${If} $NoSystem == "1"
    ReadEnvStr $R3 "KICAD_CONFIG_HOME"
    ${If} $R3 == ""
      StrCpy $NoKiCadCfg "1"
      !insertmacro UnselectSection ${SecApi}
    ${EndIf}
    ; the Freerouting download goes to %COPPER_HOME%\tools (default ~/.copper/tools)
    ReadEnvStr $R3 "COPPER_HOME"
    ${If} $R3 == ""
      !insertmacro UnselectSection ${SecFreerouting}
    ${EndIf}
  ${EndIf}

  ; KiCad (only a warning — Copper can be installed before KiCad)
  StrCpy $KiCadFound "0"
  StrCpy $KiCadPython ""
  ${If} ${FileExists} "$PROGRAMFILES64\KiCad\10.0\bin\kicad-cli.exe"
    StrCpy $KiCadFound "1"
    StrCpy $KiCadPython "$PROGRAMFILES64\KiCad\10.0\bin\python.exe"
  ${ElseIf} ${FileExists} "$PROGRAMFILES64\KiCad\9.0\bin\kicad-cli.exe"
    StrCpy $KiCadFound "1"
    StrCpy $KiCadPython "$PROGRAMFILES64\KiCad\9.0\bin\python.exe"
  ${ElseIf} ${FileExists} "$LOCALAPPDATA\Programs\KiCad\10.0\bin\kicad-cli.exe"
    StrCpy $KiCadFound "1"
    StrCpy $KiCadPython "$LOCALAPPDATA\Programs\KiCad\10.0\bin\python.exe"
  ${EndIf}
  ${IfNot} ${FileExists} $KiCadPython
    StrCpy $KiCadPython ""
  ${EndIf}
  ${If} $KiCadFound == "0"
    MessageBox MB_ICONINFORMATION|MB_OKCANCEL "KiCad 9 or 10 was not found on this PC.$\r$\n$\r$\nCopper needs KiCad to work. You can install Copper now and KiCad later (kicad.org/download), then run 'Copper doctor'." /SD IDOK IDOK +2
      Abort
  ${EndIf}

  ; AI apps: show only the ones that are installed, pre-ticked
  !insertmacro DetectPath $R4 "$RoamDir\Claude\*.*"
  ${If} $R4 == "0"
    !insertmacro DetectPath $R4 "$LocalDir\AnthropicClaude\*.*"
  ${EndIf}
  ${If} $R4 == "0"
    FindFirst $R5 $R6 "$LocalDir\Packages\Claude_*"
    FindClose $R5
    ${If} $R6 != ""
      StrCpy $R4 "1"
    ${EndIf}
  ${EndIf}
  !insertmacro ShowIf ${SecClaudeDesktop} $R4 "Claude Desktop"

  !insertmacro DetectPath $R4 "$HomeDir\.claude.json"
  ${If} $R4 == "0"
    !insertmacro DetectPath $R4 "$HomeDir\.claude\*.*"
  ${EndIf}
  !insertmacro ShowIf ${SecClaudeCode} $R4 "Claude Code"

  !insertmacro DetectPath $R4 "$HomeDir\.codex\*.*"
  !insertmacro ShowIf ${SecCodex} $R4 "OpenAI Codex"

  !insertmacro DetectPath $R4 "$HomeDir\.cursor\*.*"
  !insertmacro ShowIf ${SecCursor} $R4 "Cursor"

  !insertmacro DetectPath $R4 "$RoamDir\Code\*.*"
  ${If} $R4 == "0"
    !insertmacro DetectPath $R4 "$RoamDir\Code - Insiders\*.*"
  ${EndIf}
  !insertmacro ShowIf ${SecVSCode} $R4 "VS Code (GitHub Copilot)"

  !insertmacro DetectPath $R4 "$HomeDir\.codeium\windsurf\*.*"
  !insertmacro ShowIf ${SecWindsurf} $R4 "Windsurf / Devin"

  !insertmacro DetectPath $R4 "$RoamDir\Zed\*.*"
  !insertmacro ShowIf ${SecZed} $R4 "Zed"

  !insertmacro DetectPath $R4 "$HomeDir\.gemini\*.*"
  !insertmacro ShowIf ${SecGemini} $R4 "Gemini CLI"

  !insertmacro DetectPath $R4 "$HomeDir\.copilot\*.*"
  !insertmacro ShowIf ${SecCopilotCli} $R4 "GitHub Copilot CLI"
FunctionEnd

; ── uninstaller ────────────────────────────────────────────────────────────
Function un.onInit
  StrCpy $NoSystem "0"
  ${If} ${FileExists} "$INSTDIR\nosystem.flag"
    StrCpy $NoSystem "1"
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.StopRunningCopper
  ${If} ${FileExists} "$INSTDIR\bin\copper.exe"
    DetailPrint "Removing Copper from your AI apps…"
    nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" uninstall --client all'
    Pop $0
    ${If} $NoSystem != "1"
      nsExec::ExecToLog '"$INSTDIR\bin\copper.exe" _winpath remove "$INSTDIR\bin"'
      Pop $0
    ${EndIf}
  ${EndIf}

  ; let uv remove its own trees first (handles junctions and long paths)
  ${If} ${FileExists} "$INSTDIR\uv.exe"
    !insertmacro SetEnv "UV_TOOL_DIR"           "$INSTDIR\tools"
    !insertmacro SetEnv "UV_TOOL_BIN_DIR"       "$INSTDIR\bin"
    !insertmacro SetEnv "UV_PYTHON_INSTALL_DIR" "$INSTDIR\python"
    nsExec::ExecToLog '"$INSTDIR\uv.exe" tool uninstall copper-kicad'
    Pop $0
    nsExec::ExecToLog '"$INSTDIR\uv.exe" python uninstall --all'
    Pop $0
  ${EndIf}
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\bin"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\wheels"
  RMDir /r "$INSTDIR\python-dist"
  Delete "$INSTDIR\uv.exe"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\QUICKSTART.html"
  Delete "$INSTDIR\copper-doctor.cmd"
  Delete "$INSTDIR\copper.ico"
  Delete "$INSTDIR\nosystem.flag"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  ${If} $NoSystem != "1"
    RMDir /r "$SMPROGRAMS\Copper for KiCad"
    DeleteRegKey HKCU "${UNINST_KEY}"
    DeleteRegKey HKCU "${APP_KEY}"
  ${EndIf}
SectionEnd
