"""
packaging/monet_gui.py
~~~~~~~~~~~~~~~~~~~~~~~

PyInstaller entry point for the standalone Windows monet GUI (``monet-gui.exe``).

A frozen onefile build unpacks the package into an ephemeral temp dir
(``sys._MEIPASS``) that is wiped on exit, so monet's per-machine state — the
``.env`` the setup wizard writes, the rotating ``monet.log`` — must live
somewhere persistent and writable instead. This launcher establishes that
location **before** importing monet (``monet/__init__`` reads the log path and
loads ``.env`` at import time):

* ``MONET_HOME`` → the per-user dir, which ``monet.setup_core`` uses for the
  frozen ``.env`` path;
* ``MONET_LOG_DIR`` → same dir, so ``monet.log`` is writable;
* the process CWD is moved there, so monet's import-time ``.env`` search
  (``find_dotenv(usecwd=True)``) picks up the same file.

Then it opens the normal GUI (``MonetMainWindow``), which auto-launches the
setup wizard on first run. An optional argv[1] selects a microscope to connect.
"""

import os
import sys


def _user_dir():
    """The persistent per-user monet dir (mirrors setup_core.user_config_dir)."""
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "monet" if os.name == "nt" else ".monet")


# Modules monet loads dynamically by classpath (util.load_class), so they must
# be in the bundle even though nothing imports them statically. --selfcheck
# imports them to prove the PyInstaller build is complete, without a display.
_SELFCHECK_MODULES = (
    "monet",
    "monet.gui",
    "monet.setup_core",
    "monet.setup_wizard_qt",
    "monet.powermeter",
    "monet.attenuation",
    "monet.laser",
    "monet.analysis",
    "monet.beampath",
    "monet.control",
)


def _selfcheck():
    """Import the GUI + all dynamically-loaded drivers; print OK and exit(0).

    Headless (no QApplication), so CI can run ``monet-gui.exe --selfcheck`` to
    verify the frozen bundle actually contains monet and its drivers.
    """
    import importlib

    try:
        for name in _SELFCHECK_MODULES:
            importlib.import_module(name)
        import monet

        print("monet selfcheck OK — version", monet.__version__)
    except Exception as exc:
        # Report to stderr so a console/CI run shows which module is missing
        # from the frozen bundle, and exit non-zero so the release fails.
        print("monet selfcheck FAILED: {}".format(exc), file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def main():
    home = _user_dir()
    os.makedirs(home, exist_ok=True)
    # Must be set before `import monet` (logging + .env load happen at import).
    os.environ.setdefault("MONET_HOME", home)
    os.environ.setdefault("MONET_LOG_DIR", home)
    try:
        os.chdir(home)
    except OSError:
        pass

    if "--selfcheck" in sys.argv:
        _selfcheck()

    from PyQt6.QtWidgets import QApplication

    from monet.gui import MonetMainWindow

    name = sys.argv[1] if len(sys.argv) > 1 else None
    app = QApplication(sys.argv)
    window = MonetMainWindow(initial_microscope=name)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
