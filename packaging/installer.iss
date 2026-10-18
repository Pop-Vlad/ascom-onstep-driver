; Installer for the OnStep Alpaca driver.
;
; Deliberately a per-user install: it needs no administrator rights, so there is no UAC
; prompt at all, and "start at login" is a per-user setting anyway -- the HKCU Run key that
; the tray icon's own checkbox writes. The settings and log already live in
; %LOCALAPPDATA%\onstep-alpaca, so nothing is ever written into the install directory.
;
; Build it with packaging\build.ps1, which passes AppVersion in from pyproject.toml.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "OnStep Alpaca"
#define TrayExe "onstep-alpaca-tray.exe"
#define ConsoleExe "onstep-alpaca.exe"

[Setup]
AppId={{93FF9212-5CF4-4287-882E-F9D85899861F}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Vlad Pop
AppPublisherURL=https://github.com/Pop-Vlad/ascom-onstep-driver
AppSupportURL=https://github.com/Pop-Vlad/ascom-onstep-driver/issues
AppUpdatesURL=https://github.com/Pop-Vlad/ascom-onstep-driver/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=OnStepAlpaca-Setup-{#AppVersion}
SetupIconFile=onstep-alpaca.ico
UninstallDisplayIcon={app}\{#TrayExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; No admin rights, so no UAC prompt. /ALLUSERS still works for anyone who wants a
; machine-wide install, but it is not worth a wizard page for a single-user utility.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline

; Let Restart Manager shut down a running copy, rather than failing on a file in use when
; upgrading over an instance started at login.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startatlogin"; Description: "Start {#AppName} when I log in"; GroupDescription: "Additional options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
Source: "..\dist\onstep-alpaca\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#TrayExe}"; Comment: "Run the driver in the notification area"
Name: "{autoprograms}\{#AppName} (console)"; Filename: "{app}\{#ConsoleExe}"; Parameters: "--help"; Comment: "Command-line options, --scan and --simulate"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#TrayExe}"; Tasks: desktopicon

[Registry]
; Written in exactly the form onstep_alpaca.autostart produces, quotes included, so the
; tray icon's own checkbox and this task stay in agreement about what is registered.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "OnStep Alpaca"; ValueData: """{app}\{#TrayExe}"""; \
  Flags: uninsdeletevalue; Tasks: startatlogin
; Clearing the tick on a re-install has to actually turn it off, including an entry left by
; an earlier pip install pointing somewhere else entirely.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; \
  ValueName: "OnStep Alpaca"; Flags: deletevalue; Tasks: not startatlogin

[Run]
Filename: "{app}\{#TrayExe}"; Description: "Start {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallRun]
; The tray app is very likely running right now -- that is the point of it.
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#TrayExe}"; Flags: runhidden; RunOnceId: "StopTray"
Filename: "{sys}\taskkill.exe"; Parameters: "/f /im {#ConsoleExe}"; Flags: runhidden; RunOnceId: "StopConsole"

[Code]
procedure CurUninstallStepChanged(CurStep: TUninstallStep);
var
  Settings: String;
begin
  // Settings live outside the install directory, so they survive an uninstall unless the
  // user says otherwise. Asking is better than either silently keeping or silently
  // deleting a config somebody spent a cold night getting right.
  if CurStep = usPostUninstall then
  begin
    Settings := ExpandConstant('{localappdata}\onstep-alpaca');
    if DirExists(Settings) and (not UninstallSilent) then
      if MsgBox('Remove your {#AppName} settings and log file as well?' + #13#10#13#10
                + Settings, mbConfirmation, MB_YESNO) = IDYES then
        DelTree(Settings, True, True, True);
  end;
end;
