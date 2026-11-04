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
# (`msl.equipment.resources.thorlabs`), nidaqmx — so PyInstaller's static
# analysis would miss them and a real rig would fail at connect time with
# ModuleNotFoundError. Pull each in explicitly.
#
# One table, two keys, so there is a single place to edit when an SDK is
# added/renamed (avoids the import-name vs distribution-name drift that let the
# `nitypes` metadata go missing once):
#   * import_name  → collect_submodules / collect_data_files (bundle the code)
#   * dist_name    → copy_metadata(recursive=True) (bundle the .dist-info some
#                    packages read via importlib.metadata at import; e.g.
#                    nidaqmx → nitypes `version("nitypes")`)
# Either may be None. All best-effort: a package absent from a given build
# (e.g. a [gui]-only build) just contributes nothing instead of failing.
_HARDWARE_SDKS = (
    # (import_name, distribution_name)
    ("msl", "msl-equipment"),
    ("pyvisa", "PyVISA"),
    ("nidaqmx", "nidaqmx"),
    (None, "nitypes"),  # nidaqmx dep; code pulled by analysis, metadata not
    ("pycobolt", "pycobolt"),
    ("microscope", "microscope"),
    ("pycromanager", "pycromanager"),
    ("ThorlabsPM100", "ThorlabsPM100"),
    ("serial", None),  # pyserial — base dep, imported dynamically by drivers
)
for _imp, _dist in _HARDWARE_SDKS:
    if _imp:
        try:
            hiddenimports += collect_submodules(_imp)
        except Exception:
            pass
        try:
            datas += collect_data_files(_imp)
        except Exception:
            pass
    if _dist:
        try:
            datas += copy_metadata(_dist, recursive=True)
        except Exception:
            pass

# The optional [setup] extra: paramiko powers the setup wizard's automated SSH
# server install. It is lazy-imported (setup_core.remote_install_server), so
# pull it in explicitly; its binary dependencies (cryptography, bcrypt, pynacl)
# are handled by PyInstaller's bundled hooks. copy_metadata(recursive) ships the
# .dist-info several of these read at import.
try:
    hiddenimports += collect_submodules("paramiko")
except Exception:
    pass
try:
    datas += copy_metadata("paramiko", recursive=True)
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
