# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the standalone Windows monet GUI (``monet-gui.exe``).

Build (on Windows, with the package + PyInstaller installed):

    pip install ".[gui]" pyinstaller
    pyinstaller --clean --noconfirm packaging/monet-gui.spec

Produces ``dist/monet-gui.exe`` (onefile, windowed). The CI release workflow
(.github/workflows/release-windows.yml) runs exactly this on a ``v*`` tag and
attaches the exe to the GitHub Release.

Notes
-----
* ``collect_submodules('monet')`` is essential: monet loads its hardware drivers
  dynamically by classpath string (``util.load_class`` -> ``import_module``),
  which PyInstaller's static analysis cannot see, so the driver modules would be
  missing without this.
* ``docs/examples`` is bundled at the same relative path the package expects
  (``setup_core.examples_dir`` resolves it via the package root, which is
  ``sys._MEIPASS`` when frozen), so the wizard's "copy templates" step works.
* The vendored Thorlabs ``monet/TLPM.py`` is pulled in by the monet collection;
  the TLPM meter still needs Thorlabs' DLL installed on the machine at runtime.
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH is injected by PyInstaller = the directory holding this spec, so the
# build works regardless of the invocation CWD.
_launcher = os.path.join(SPECPATH, "monet_gui.py")
_examples = os.path.join(SPECPATH, "..", "docs", "examples")

# Collect monet's submodules so the dynamically-imported drivers are bundled,
# but drop (a) the test package and (b) the server-only modules. The GUI never
# imports server/dashboard/schemas/serviceauth, and they import fastapi /
# pydantic / picasso_registry at module level — not in the [gui] install — so
# collecting them would bundle dead, unimportable modules and spew missing-import
# warnings. (monet.models stays: the GUI's io -> cache -> models path needs it.)
_SKIP = (
    "monet.tests",
    "monet.server",
    "monet.dashboard",
    "monet.schemas",
    "monet.serviceauth",
    "monet.migrate",
)
hiddenimports = [
    m
    for m in collect_submodules("monet")
    if not any(m == s or m.startswith(s + ".") for s in _SKIP)
]

datas = [(_examples, "docs/examples")]
datas += collect_data_files("matplotlib")

a = Analysis(
    [_launcher],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim heavyweight libs that are only needed for the [hardware]/[server]
    # extras and are not part of the GUI build, so the exe stays smaller.
    excludes=["tkinter", "fastapi", "uvicorn", "nidaqmx", "pycromanager"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="monet-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is known to corrupt PyQt6's Qt6 DLLs on Windows (crash on launch), and
    # the size win isn't worth that risk for a lab tool.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
