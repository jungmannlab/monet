"""The admin console runs from a laptop with only ``monet[admin]`` installed.

The first-class use case (C50) is administering the central host from an admin
workstation that is NOT a rig and NOT the server: ``pip install 'monet[admin]'``
(PyQt6 + paramiko) is the only install there. So importing/launching the
console must not pull the monet **server** (fastapi), any **hardware** SDK, or
even paramiko (which is lazy-imported only when a connection is actually made).

We assert this in a *subprocess* with a clean interpreter, so another test that
already imported fastapi/paramiko in this process can't mask a real import.
"""

from __future__ import annotations

import subprocess
import sys


def _import_asserting_absent(import_stmt: str) -> None:
    # Import the admin front-end, then assert the heavy stacks were not dragged
    # in. A non-zero exit (AssertionError/ImportError) fails the test with the
    # child's stderr attached.
    code = (
        f"{import_stmt}\n"
        "import sys\n"
        "heavy = [m for m in ('fastapi', 'paramiko', 'monet.server',\n"
        "    'pyvisa', 'pycromanager', 'nidaqmx') if m in sys.modules]\n"
        "assert not heavy, 'admin import pulled: ' + ', '.join(heavy)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_core_import_pulls_no_server_hardware_or_paramiko():
    _import_asserting_absent("import monet.admin.core")


def test_cli_import_pulls_no_server_hardware_or_paramiko():
    _import_asserting_absent("import monet.admin.cli")


def test_admin_core_importable_without_paramiko():
    # The core module must import even when paramiko is absent (it degrades to
    # an AdminError only when a connection is attempted). Simulate absence by
    # blocking the import in a child interpreter.
    code = (
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    if name == 'paramiko' or name.startswith('paramiko.'):\n"
        "        raise ModuleNotFoundError('paramiko blocked for test')\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "import monet.admin.core as c\n"
        "a = c.MonetAdmin('h', 'u')\n"
        "try:\n"
        "    a.connect()\n"
        "    raise SystemExit('expected AdminError')\n"
        "except c.AdminError as e:\n"
        "    assert 'paramiko' in str(e)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
