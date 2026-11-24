; Inno Setup script for the standalone Windows monet GUI installer.
;
; Produces Monet-Setup-<version>.exe, which *installs* the onedir bundle built
; by packaging/monet-gui.spec (the folder dist\monet-gui\ with monet-gui.exe
; inside) per-user — no admin rights — and creates a proper "Monet GUI"
; Start-menu shortcut, an uninstaller, and an Add/Remove Programs entry. After
; running the installer the GUI is actually installed and launches from the
; Start menu by name (no per-launch temp extraction — see the spec).
;
; Build locally (after the onedir folder dist\monet-gui\ exists):
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
; The standalone monet.ico (installed below) — not the exe, whose embedded
; icon we dropped to avoid AV false positives on the unsigned bootloader.
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
; Onedir build: install the whole dist\monet-gui\ folder (exe + its _internal
; dependencies) so there is no per-launch temp extraction.
Source: "..\dist\monet-gui\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Standalone app icon for the shortcuts (the exe carries no embedded icon).
Source: "..\monet\icons\monet.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; IconFilename points the shortcut at the standalone monet.ico (the exe has no
; embedded icon), so the Start-menu and desktop shortcuts show the Monet logo.
Name: "{autoprograms}\Monet GUI"; Filename: "{app}\monet-gui.exe"; IconFilename: "{app}\monet.ico"
Name: "{autodesktop}\Monet GUI"; Filename: "{app}\monet-gui.exe"; IconFilename: "{app}\monet.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\monet-gui.exe"; Description: "Launch Monet GUI now"; Flags: nowait postinstall skipifsilent
