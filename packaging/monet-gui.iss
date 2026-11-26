; Inno Setup script for the standalone Windows monet GUI installer.
;
; Produces Monet-Setup-<version>.exe, which *installs* the embeddable-CPython
; bundle built by packaging/build-embedded.ps1 (the folder dist\monet-gui\ with
; the signed python.exe / pythonw.exe and a real monet install inside) per-user
; — no admin rights — and creates a proper "Monet GUI" Start-menu shortcut, an
; uninstaller, and an Add/Remove Programs entry. The shortcut runs
; `pythonw.exe monet_gui.py`, so there is NO custom unsigned .exe for Apex One's
; TrendX engine to false-positive (that quarantined the old PyInstaller
; monet-gui.exe).
;
; Build locally (after the bundle dist\monet-gui\ exists):
;     pwsh packaging\build-embedded.ps1
;     iscc /DMyAppVersion=0.6.1 packaging\monet-gui.iss
;
; The CI release workflow (.github/workflows/release-windows.yml) runs exactly
; this on a v* tag and attaches Monet-Setup-<version>.exe to the GitHub Release.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
; A fixed AppId so a new version upgrades the previous install in place
; (rather than installing side-by-side). Never change this GUID.
AppId={{A7C3E2F1-5B4D-4E6A-9C1F-2D3B4A5C6D7E}
AppName=Monet
AppVersion={#MyAppVersion}
AppVerName=Monet {#MyAppVersion}
AppPublisher=Jungmann Lab, MPI of Biochemistry
AppPublisherURL=https://github.com/jungmannlab/monet
AppSupportURL=https://github.com/jungmannlab/monet/issues
; Per-user install by default (no admin prompt); an admin may opt into an
; all-users install via the privileges dialog.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\Monet
DefaultGroupName=Monet
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE.md
OutputDir=..\dist
OutputBaseFilename=Monet-Setup-{#MyAppVersion}
; The standalone monet.ico (shipped inside the bundle by build-embedded.ps1).
UninstallDisplayIcon={app}\monet.ico
UninstallDisplayName=Monet {#MyAppVersion}
; Installer's own icon (the Monet logo). This icons Setup.exe, not the app.
SetupIconFile=..\monet\icons\monet.ico
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Install the whole embeddable-CPython bundle folder (python.exe / pythonw.exe,
; the stdlib, Lib\site-packages with monet + drivers, monet_gui.py, monet.ico).
Source: "..\dist\monet-gui\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Launch via the bundled (PSF-signed) pythonw.exe running the monet_gui.py
; launcher — no custom .exe. IconFilename gives the shortcut the Monet logo.
Name: "{autoprograms}\Monet GUI"; Filename: "{app}\pythonw.exe"; Parameters: """{app}\monet_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\monet.ico"
Name: "{autodesktop}\Monet GUI"; Filename: "{app}\pythonw.exe"; Parameters: """{app}\monet_gui.py"""; WorkingDir: "{app}"; IconFilename: "{app}\monet.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\pythonw.exe"; Parameters: """{app}\monet_gui.py"""; WorkingDir: "{app}"; Description: "Launch Monet GUI now"; Flags: nowait postinstall skipifsilent
