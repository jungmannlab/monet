; Inno Setup script for the standalone Windows monet GUI installer.
;
; Produces Monet-Setup-<version>.exe, which *installs* the portable
; monet-gui.exe (from packaging/monet-gui.spec) per-user — no admin rights —
; and creates a proper "Monet GUI" Start-menu shortcut, an uninstaller, and an
; Add/Remove Programs entry. This replaces the "download the bare exe and run
; it from Downloads" flow: after running the installer the GUI is actually
; installed and launches from the Start menu by name.
;
; Build locally (after dist\monet-gui.exe exists):
;     pip install ".[gui]" pyinstaller
;     pyinstaller --clean --noconfirm packaging/monet-gui.spec
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
UninstallDisplayIcon={app}\monet-gui.exe
UninstallDisplayName=Monet {#MyAppVersion}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\monet-gui.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Monet GUI"; Filename: "{app}\monet-gui.exe"
Name: "{autodesktop}\Monet GUI"; Filename: "{app}\monet-gui.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\monet-gui.exe"; Description: "Launch Monet GUI now"; Flags: nowait postinstall skipifsilent
