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

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

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

# Hardware SDK packages (the [hardware] extra). monet's drivers lazy-import
# these by name *inside methods* (e.g. attenuation.py's KinesisAttenuator does
# `from msl.equipment import ...` only when it connects), and several resolve
# submodules dynamically — msl-equipment's resource backends
# (`msl.equipment.resources.thorlabs`), pyvisa/pyvisa-py backends, nidaqmx — so
# PyInstaller's static analysis would miss them and a real rig would fail at
# connect time with ModuleNotFoundError. Pull each in explicitly. Best-effort:
# a package not present in a given build (e.g. a [gui]-only build) just
# contributes nothing instead of failing the spec.
_HARDWARE_PKGS = (
    "msl",
    "pyvisa",
    "pyvisa_py",
    "nidaqmx",
    "pycobolt",
    "microscope",
    "pycromanager",
    "ThorlabsPM100",
    "serial",  # pyserial — base dep, but several drivers import it dynamically
)
for _pkg in _HARDWARE_PKGS:
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        pass
    try:
        datas += collect_data_files(_pkg)
    except Exception:
        pass

# Some SDKs read their own (or a dependency's) distribution metadata at import
# via importlib.metadata — e.g. nidaqmx pulls in `nitypes`, which calls
# ``version("nitypes")`` on import and dies with "No package metadata was found"
# if the .dist-info isn't bundled. ``copy_metadata(..., recursive=True)`` ships
# each package's metadata and that of its dependencies. Distribution names (not
# import names) are required here; best-effort so a missing package is skipped.
_METADATA_DISTS = (
    "nidaqmx",
    "nitypes",
    "PyVISA",
    "PyVISA-py",
    "pycromanager",
    "microscope",
    "ThorlabsPM100",
    "msl-equipment",
    "pycobolt",
)
for _dist in _METADATA_DISTS:
    try:
        datas += copy_metadata(_dist, recursive=True)
    except Exception:
        pass

a = Analysis(
    [_launcher],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim libs that aren't part of this build: the [server] web stack (the GUI
    # never imports it) and tkinter (matplotlib's Qt backend is used, not Tk).
    # nidaqmx / pycromanager are NO LONGER excluded — the installer now bundles
    # the [hardware] extra so a real rig can connect.
    excludes=["tkinter", "fastapi", "uvicorn"],
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
