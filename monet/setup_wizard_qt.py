"""
monet/setup_wizard_qt.py
~~~~~~~~~~~~~~~~~~~~~~~~~~

PyQt6 ``QWizard`` front-end for first-time onboarding — the GUI twin of
``monet setup`` (:mod:`monet.setup_cli`). Both share :mod:`monet.setup_core`, so
they behave identically; this module only renders the pages and collects input.

``MonetMainWindow`` opens :func:`maybe_run_setup` on first launch (no ``.env`` and
only built-in configs) and exposes a "Setup Wizard…" menu action to re-run it.
Imported lazily so a no-Qt install is unaffected.

:authors: Heinrich Grabmayr, 2026
:copyright: Copyright (c) 2026 Jungmann Lab, MPI of Biochemistry
"""

import logging
import os
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from monet import setup_core as sc

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Background worker so the (blocking) SSH install never freezes the UI thread
# ──────────────────────────────────────────────────────────────────────────
class _SshInstallWorker(QThread):
    """Runs ``remote_install_server`` off the GUI thread; streams via signals."""

    output = pyqtSignal(str)
    done = pyqtSignal(object)  # the {'url', 'token'} result
    failed = pyqtSignal(str)

    def __init__(self, kwargs, parent=None):
        super().__init__(parent)
        self._kwargs = kwargs

    def run(self):
        try:
            result = sc.remote_install_server(
                on_output=self.output.emit, **self._kwargs
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


# ──────────────────────────────────────────────────────────────────────────
# A reusable driver picker + dynamic kwargs form for one config section
# ──────────────────────────────────────────────────────────────────────────
class _DriverForm(QWidget):
    """A combo of the drivers in ``category`` + a form for the chosen one."""

    def __init__(self, category, parent=None):
        super().__init__(parent)
        self.category = category
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for name, drv in sc.DRIVERS[category].items():
            self.combo.addItem(drv.label, name)
        lay.addWidget(self.combo)
        self._form_host = QWidget()
        self._form = QFormLayout(self._form_host)
        lay.addWidget(self._form_host)
        self._widgets = {}
        # Cache device enumeration per tag for this form's lifetime: a scan can
        # hit pyvisa/nidaqmx (slow, hardware I/O on the UI thread), and the
        # combo rebuilds on every driver switch.
        self._enum_cache = {}
        self.combo.currentIndexChanged.connect(self._rebuild)
        self._rebuild()

    def _choices(self, tag):
        if tag not in self._enum_cache:
            self._enum_cache[tag] = sc.enumerate_choices(tag)
        return self._enum_cache[tag]

    def current_name(self):
        return self.combo.currentData()

    def _rebuild(self):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._widgets = {}
        drv = sc.DRIVERS[self.category][self.current_name()]
        for f in drv.fields:
            edit = QLineEdit()
            if f.default is not None:
                edit.setText(str(f.default))
            tips = [f.help] if f.help else []
            if f.choices_from:
                found = self._choices(f.choices_from)
                if found:
                    tips.append("detected: " + ", ".join(map(str, found)))
            if tips:
                edit.setToolTip(" — ".join(tips))
            label = f.name + (" *" if f.required else "")
            self._form.addRow(label, edit)
            self._widgets[f.name] = (f, edit)

    def missing_required(self):
        """Return the names of required fields left blank."""
        return [
            name
            for name, (f, edit) in self._widgets.items()
            if f.required and not edit.text().strip()
        ]

    def section(self):
        """Return ``{'class': name, 'init_kwargs': {...}}`` for this section."""
        kwargs = {}
        for name, (f, edit) in self._widgets.items():
            raw = edit.text().strip()
            if raw == "":
                if f.default is None:
                    continue
                kwargs[name] = f.default
            else:
                kwargs[name] = sc.coerce_value(raw, f.type)
        return {"class": self.current_name(), "init_kwargs": kwargs}


# ──────────────────────────────────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────────────────────────────────
class _IntroPage(QWizardPage):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.setTitle("Welcome to monet setup")
        lay = QVBoxLayout(self)
        if state.configured:
            where = state.configs_path or state.env_path
            msg = (
                "This machine already has configuration ({}). The wizard will "
                "update it.".format(where)
            )
        else:
            msg = (
                "This machine is not configured yet. This wizard connects it to "
                "a calibration server (or helps you stand one up), points it at "
                "your config files, and sets up a microscope."
            )
        label = QLabel(msg)
        label.setWordWrap(True)
        lay.addWidget(label)
        lay.addWidget(
            QLabel("Settings are written to:\n{}".format(state.env_path))
        )


class _ServerPage(QWizardPage):
    """Connect to an existing server, or stand up a new one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Calibration server")
        self._worker = None
        lay = QVBoxLayout(self)

        self.rb_existing = QRadioButton("Connect to an existing server")
        self.rb_local = QRadioButton(
            "Single computer — local database file (no server)"
        )
        self.rb_new = QRadioButton("Set up a new server (another machine)")
        self.rb_existing.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.rb_existing, 0)
        group.addButton(self.rb_local, 1)
        group.addButton(self.rb_new, 2)
        lay.addWidget(self.rb_existing)
        lay.addWidget(self.rb_local)
        lay.addWidget(self.rb_new)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_existing())
        self.stack.addWidget(self._build_local())
        self.stack.addWidget(self._build_new())
        lay.addWidget(self.stack)
        group.idToggled.connect(
            lambda i, on: self.stack.setCurrentIndex(i) if on else None
        )

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def _build_existing(self):
        w = QWidget()
        form = QFormLayout(w)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http://host:8000")
        form.addRow("Server URL", self.url_edit)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Token (if required)", self.token_edit)
        btn = QPushButton("Test connection")
        btn.clicked.connect(self._test_existing)
        form.addRow("", btn)
        return w

    def _build_local(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        msg = QLabel(
            "This machine will use a local database file (an .xlsx on this "
            "computer) — no server, no token, no network. You'll set the file "
            "on the Microscope page. Pick this for a single-computer lab."
        )
        msg.setWordWrap(True)
        lay.addWidget(msg)
        return w

    def _build_new(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("linux-server.lab")
        form.addRow("Server host", self.host_edit)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(8000)
        form.addRow("Port", self.port_spin)
        self.rig_edit = QLineEdit("acquisition-rig")
        form.addRow("Rig label (for its token)", self.rig_edit)
        lay.addLayout(form)

        ssh_box = QGroupBox("Automated install over SSH (optional)")
        ssh_form = QFormLayout(ssh_box)
        self.ssh_user = QLineEdit()
        ssh_form.addRow("SSH user", self.ssh_user)
        self.ssh_key = QLineEdit()
        self.ssh_key.setPlaceholderText("private-key file (blank = password)")
        ssh_form.addRow("SSH key file", self.ssh_key)
        self.ssh_pw = QLineEdit()
        self.ssh_pw.setEchoMode(QLineEdit.EchoMode.Password)
        ssh_form.addRow("SSH password", self.ssh_pw)
        row = QHBoxLayout()
        btn_install = QPushButton("Install over SSH")
        btn_install.clicked.connect(self._install_ssh)
        btn_manual = QPushButton("Show manual steps")
        btn_manual.clicked.connect(self._show_manual)
        row.addWidget(btn_install)
        row.addWidget(btn_manual)
        ssh_form.addRow(row)
        lay.addWidget(ssh_box)

        self.new_token = QLineEdit()
        self.new_token.setEchoMode(QLineEdit.EchoMode.Password)
        tok_form = QFormLayout()
        tok_form.addRow("Token (paste after install)", self.new_token)
        lay.addLayout(tok_form)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log)
        return w

    # -- existing-server actions --
    def _test_existing(self):
        url = self.url_edit.text().strip().rstrip("/")
        if not url:
            self.status.setText("Enter a server URL first.")
            return
        token = self.token_edit.text().strip()
        if token:
            os.environ[sc.ENV_CLIENT_TOKEN] = token
        verdict = sc.check_server(url)
        self.status.setText(
            "Reachable: {} — {}".format(
                "yes" if verdict["reachable"] else "NO", verdict["detail"]
            )
        )

    # -- new-server actions --
    def _show_manual(self):
        self.log.setPlainText(
            sc.guided_server_instructions(
                host=self.host_edit.text().strip() or "<server>",
                port=self.port_spin.value(),
                rig=self.rig_edit.text().strip() or "this-rig",
            )
        )

    def _install_ssh(self):
        host = self.host_edit.text().strip()
        user = self.ssh_user.text().strip()
        if not host or not user:
            self.log.append("Enter server host and SSH user first.\n")
            return
        if self._worker is not None and self._worker.isRunning():
            self.log.append("Install already running…")
            return
        self.log.append("Installing on {}@{} …".format(user, host))
        self.setCursor(Qt.CursorShape.WaitCursor)
        # Run off the GUI thread: SSH connect + script + pip can take tens of
        # seconds and would otherwise freeze the wizard.
        self._worker = _SshInstallWorker(
            dict(
                host=host,
                user=user,
                password=self.ssh_pw.text() or None,
                key_filename=self.ssh_key.text().strip() or None,
                port=self.port_spin.value(),
                rig=self.rig_edit.text().strip() or "acquisition-rig",
            )
        )
        self._worker.output.connect(lambda s: self.log.append(s.rstrip("\n")))
        self._worker.done.connect(self._on_install_done)
        self._worker.failed.connect(self._on_install_failed)
        self._worker.start()

    def _on_install_done(self, result):
        self.unsetCursor()
        self.new_token.setText(result["token"])
        self.log.append("Done — token captured. Click Next.")

    def _on_install_failed(self, msg):
        self.unsetCursor()
        self.log.append("Failed: {}\nUse the manual steps.".format(msg))

    def validatePage(self):
        wiz = self.wizard()
        if self.rb_local.isChecked():
            # Single computer: no server — the microscope uses a local file.
            wiz.server_url = ""
            return True
        if self.rb_existing.isChecked():
            url = self.url_edit.text().strip().rstrip("/")
            token = self.token_edit.text().strip()
            probe = url if "://" in url else "http://" + url
            if not url or not urlparse(probe).hostname:
                self.status.setText("Enter a valid server URL (with a host).")
                return False
        else:
            host = self.host_edit.text().strip()
            if not host:
                self.status.setText("Enter the server host.")
                return False
            url = "http://{}:{}".format(host, self.port_spin.value())
            token = self.new_token.text().strip()
        wiz.server_url = url
        if token:
            os.environ[sc.ENV_CLIENT_TOKEN] = token
            wiz.updates[sc.ENV_CLIENT_TOKEN] = token
            wiz.updates[sc.ENV_AUTH] = "on"
        return True


class _CentralFolderPage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Config & protocol files")
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "Where are (or should be) configs.yaml and protocols.yaml? "
                "Use a shared network folder to reuse the lab's configs."
            )
        )
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        btn = QPushButton("Browse…")
        btn.clicked.connect(self._browse)
        row.addWidget(self.folder_edit)
        row.addWidget(btn)
        lay.addLayout(row)
        self.scaffold_cb = QCheckBox(
            "Copy example templates here to start from"
        )
        lay.addWidget(self.scaffold_cb)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            self.folder_edit.setText(folder)

    def validatePage(self):
        folder = os.path.expanduser(self.folder_edit.text().strip())
        if not folder:
            self.status.setText("Choose a folder.")
            return False
        configs_path = os.path.join(folder, "configs.yaml")
        protocols_path = os.path.join(folder, "protocols.yaml")
        if self.scaffold_cb.isChecked():
            try:
                paths = sc.scaffold_central_folder(folder)
            except FileNotFoundError as exc:
                self.status.setText(
                    "Could not copy templates: {} — continuing with empty "
                    "paths to author by hand.".format(exc)
                )
            else:
                configs_path = paths["configs"]
                protocols_path = paths["protocols"]
        wiz = self.wizard()
        wiz.configs_path = configs_path
        wiz.protocols_path = protocols_path
        wiz.updates[sc.ENV_CONFIG_PATHS] = sc.paths_to_env([configs_path])
        wiz.updates[sc.ENV_PROTOCOL_PATHS] = sc.paths_to_env([protocols_path])
        return True


def _confirm_overwrite(parent, name, path, kind):
    """True if it's safe to write ``name`` into ``path`` (no collision, or the
    user confirms overwriting an existing same-named entry)."""
    if not name or name not in sc.existing_entry_names(path):
        return True
    reply = QMessageBox.question(
        parent,
        "Overwrite?",
        "A {} named '{}' already exists in\n{}\n\nOverwrite it?".format(
            kind, name, path
        ),
    )
    return reply == QMessageBox.StandardButton.Yes


class _LaserRow(QWidget):
    """One laser: wavelength + a driver picker (reuses _DriverForm)."""

    def __init__(self, on_remove, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.wl = QSpinBox()
        self.wl.setRange(1, 10000)
        self.wl.setValue(488)
        self.form = _DriverForm("laser")
        btn = QPushButton("✕")
        btn.setFixedWidth(28)
        btn.clicked.connect(lambda: on_remove(self))
        lay.addWidget(QLabel("λ [nm]"))
        lay.addWidget(self.wl)
        lay.addWidget(self.form, 1)
        lay.addWidget(btn)


class _BeamPathRow(QWidget):
    """One beam-path device: object id + a driver picker."""

    def __init__(self, on_remove, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.obj = QLineEdit()
        self.obj.setPlaceholderText("id, e.g. DC")
        self.obj.setFixedWidth(100)
        self.form = _DriverForm("beampath")
        btn = QPushButton("✕")
        btn.setFixedWidth(28)
        btn.clicked.connect(lambda: on_remove(self))
        lay.addWidget(self.obj)
        lay.addWidget(self.form, 1)
        lay.addWidget(btn)


class _MicroscopePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Microscope")
        self._laser_rows = []
        self._bp_rows = []
        lay = QVBoxLayout(self)
        self.author_cb = QCheckBox("Author a microscope entry now")
        self.author_cb.setChecked(True)
        lay.addWidget(self.author_cb)

        self.body = QWidget()
        body_lay = QVBoxLayout(self.body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        form.addRow("Name", self.name_edit)
        self.db_edit = QLineEdit()
        form.addRow("Database (server URL or .xlsx)", self.db_edit)
        self.wl_spin = QSpinBox()
        self.wl_spin.setRange(1, 10000)
        self.wl_spin.setValue(488)
        form.addRow("Index wavelength [nm]", self.wl_spin)
        self.lp_spin = QSpinBox()
        self.lp_spin.setRange(0, 100000)
        self.lp_spin.setValue(100)
        form.addRow("Index laser_power [mW]", self.lp_spin)
        self.pm_form = _DriverForm("powermeter")
        self.at_form = _DriverForm("attenuation")
        self.an_form = _DriverForm("analysis")
        form.addRow("Power meter", self.pm_form)
        form.addRow("Attenuator", self.at_form)
        form.addRow("Curve model", self.an_form)
        body_lay.addLayout(form)

        # Lasers (optional; needed for multi-laser protocols)
        laser_box = QGroupBox(
            "Lasers (optional — enables a calibration protocol)"
        )
        lb = QVBoxLayout(laser_box)
        self._laser_host = QVBoxLayout()
        lb.addLayout(self._laser_host)
        add_laser = QPushButton("Add laser")
        add_laser.clicked.connect(self._add_laser)
        lb.addWidget(add_laser)
        body_lay.addWidget(laser_box)

        # Beam-path devices (optional; Micro-Manager)
        bp_box = QGroupBox("Beam-path devices (optional — Micro-Manager)")
        bb = QVBoxLayout(bp_box)
        self._bp_host = QVBoxLayout()
        bb.addLayout(self._bp_host)
        add_bp = QPushButton("Add beam-path device")
        add_bp.clicked.connect(self._add_bp)
        bb.addWidget(add_bp)
        body_lay.addWidget(bp_box)

        row = QHBoxLayout()
        btn_test = QPushButton("Test connection")
        btn_test.clicked.connect(self._test)
        row.addWidget(btn_test)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        row.addWidget(self.test_label, 1)
        body_lay.addLayout(row)

        lay.addWidget(self.body)
        self.author_cb.toggled.connect(self.body.setEnabled)

    def _add_laser(self):
        row = _LaserRow(self._remove_row)
        self._laser_rows.append(row)
        self._laser_host.addWidget(row)

    def _add_bp(self):
        row = _BeamPathRow(self._remove_row)
        self._bp_rows.append(row)
        self._bp_host.addWidget(row)

    def _remove_row(self, row):
        for lst in (self._laser_rows, self._bp_rows):
            if row in lst:
                lst.remove(row)
        row.setParent(None)
        row.deleteLater()

    def initializePage(self):
        if not self.db_edit.text().strip():
            url = getattr(self.wizard(), "server_url", "") or ""
            # No server (single-computer) -> default to a local database file.
            self.db_edit.setText(url or "./power_database.xlsx")

    def _answers(self):
        answers = {
            "name": self.name_edit.text().strip() or "microscope",
            "database": self.db_edit.text().strip() or "./power_database.xlsx",
            "wavelength": self.wl_spin.value(),
            "laser_power": self.lp_spin.value(),
            "powermeter": self.pm_form.section(),
            "attenuation": self.at_form.section(),
            "analysis": self.an_form.section(),
        }
        lasers = {r.wl.value(): r.form.section() for r in self._laser_rows}
        if lasers:
            answers["lasers"] = lasers
        beampath = {
            r.obj.text().strip(): r.form.section()
            for r in self._bp_rows
            if r.obj.text().strip()
        }
        if beampath:
            answers["beampath"] = beampath
        return answers

    def _build_entry(self):
        return sc.build_microscope_entry(self._answers())

    def _test(self):
        try:
            results = sc.test_microscope(self._build_entry())
        except Exception as exc:
            self.test_label.setText("Could not build config: {}".format(exc))
            return
        self.test_label.setText(
            "\n".join(
                "[{}] {}: {}".format(
                    "OK" if r.ok else "!!", r.device, r.detail
                )
                for r in results
            )
        )

    def validatePage(self):
        wiz = self.wizard()
        if not self.author_cb.isChecked():
            wiz.microscope_name = None
            wiz.microscope_entry = None
            wiz.microscope_answers = None
            return True
        if not self.name_edit.text().strip():
            self.test_label.setText("A microscope name is required.")
            return False
        missing = (
            self.pm_form.missing_required()
            + self.at_form.missing_required()
            + self.an_form.missing_required()
        )
        for r in self._laser_rows:
            missing += r.form.missing_required()
        if missing:
            self.test_label.setText(
                "Fill required fields: {}".format(", ".join(missing))
            )
            return False
        name = self.name_edit.text().strip()
        if not _confirm_overwrite(self, name, wiz.configs_path, "microscope"):
            self.test_label.setText(
                "Choose a different name to avoid overwriting '{}'.".format(
                    name
                )
            )
            return False
        answers = self._answers()
        wiz.microscope_name = name
        wiz.microscope_answers = answers
        wiz.microscope_entry = sc.build_microscope_entry(answers)
        return True


class _ProtocolPage(QWizardPage):
    """Author a multi-laser calibration sweep, with MM-.cfg preset pick-lists."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Calibration protocol")
        self._cfg = None
        self._power_edits = {}
        self._group_combos = {}  # obj -> QComboBox (which MM group drives it)
        self._bp_widgets = {}  # (laser|'end', obj) -> QComboBox | QCheckBox
        lay = QVBoxLayout(self)
        self.author_cb = QCheckBox(
            "Author a calibration protocol (multi-laser sweep) now"
        )
        self.author_cb.setChecked(True)
        lay.addWidget(self.author_cb)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        self.body = QWidget()
        body_lay = QVBoxLayout(self.body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.addWidget(
            QLabel("Power set-points [mW] per laser (comma-separated):")
        )
        self._powers_host = QWidget()
        self._powers_form = QFormLayout(self._powers_host)
        body_lay.addWidget(self._powers_host)

        cfg_row = QHBoxLayout()
        self.cfg_edit = QLineEdit()
        self.cfg_edit.setPlaceholderText(
            "Micro-Manager .cfg (optional — enables preset pick-lists)"
        )
        btn_browse = QPushButton("Browse…")
        btn_browse.clicked.connect(self._browse)
        btn_load = QPushButton("Load presets")
        btn_load.clicked.connect(self._load_cfg)
        cfg_row.addWidget(self.cfg_edit)
        cfg_row.addWidget(btn_browse)
        cfg_row.addWidget(btn_load)
        body_lay.addLayout(cfg_row)
        self.cfg_status = QLabel("")
        self.cfg_status.setWordWrap(True)
        body_lay.addWidget(self.cfg_status)

        self._bp_host = QWidget()
        self._bp_layout = QVBoxLayout(self._bp_host)
        self._bp_layout.setContentsMargins(0, 0, 0, 0)
        body_lay.addWidget(self._bp_host)

        lay.addWidget(self.body)
        self.author_cb.toggled.connect(self.body.setEnabled)

    def _lasers(self):
        answers = getattr(self.wizard(), "microscope_answers", None) or {}
        return sorted(answers.get("lasers") or {})

    def _bp_objs(self):
        answers = getattr(self.wizard(), "microscope_answers", None) or {}
        return answers.get("beampath") or {}

    def initializePage(self):
        while self._powers_form.rowCount():
            self._powers_form.removeRow(0)
        self._power_edits = {}
        self._clear_bp_grid()
        lasers = self._lasers()
        if not lasers:
            self.author_cb.setChecked(False)
            self.author_cb.setEnabled(False)
            self.body.setEnabled(False)
            self.info.setText(
                "No lasers configured on the previous page — nothing to "
                "author here. Click Next."
            )
            return
        self.author_cb.setEnabled(True)
        self.body.setEnabled(self.author_cb.isChecked())
        self.info.setText("")
        for wl in lasers:
            edit = QLineEdit("100,200,500,1000")
            self._powers_form.addRow("{} nm".format(wl), edit)
            self._power_edits[wl] = edit
        if self._bp_objs():
            self.cfg_status.setText(
                "Load a .cfg to pick beam-path presets (optional)."
            )

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Micro-Manager .cfg", "", "MM config (*.cfg);;All (*)"
        )
        if path:
            self.cfg_edit.setText(path)

    def _clear_bp_grid(self):
        while self._bp_layout.count():
            item = self._bp_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._group_combos = {}
        self._bp_widgets = {}

    def _load_cfg(self):
        path = self.cfg_edit.text().strip()
        if not path:
            self.cfg_status.setText("Enter a .cfg path first.")
            return
        try:
            self._cfg = sc.parse_mm_config(os.path.expanduser(path))
        except OSError as exc:
            self.cfg_status.setText("Could not read .cfg: {}".format(exc))
            return
        self.cfg_status.setText(
            "Groups: {} | core shutter: {}".format(
                ", ".join(self._cfg.groups) or "(none)",
                self._cfg.core_shutter or "(none)",
            )
        )
        self._build_bp_grid()

    def _build_bp_grid(self):
        self._clear_bp_grid()
        bp_objs = self._bp_objs()
        if not bp_objs or not self._cfg:
            return
        # One "which MM group drives this object" picker per preset-driven
        # object. Only groups that actually have presets are offered (an empty
        # group would yield an empty preset combo).
        usable = [g for g in self._cfg.groups if self._cfg.groups[g]]
        for obj, spec in bp_objs.items():
            if spec["class"] in sc.SHUTTER_BEAMPATH_CLASSES:
                continue
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            combo = QComboBox()
            for g in usable:
                combo.addItem(g)
            combo.currentTextChanged.connect(self._refill_presets)
            self._group_combos[obj] = combo
            row.addWidget(QLabel("MM group for '{}'".format(obj)))
            row.addWidget(combo, 1)
            self._bp_layout.addWidget(holder)
        # A preset combo / shutter checkbox per (laser|end, object).
        for key in list(self._lasers()) + ["end"]:
            label = (
                "end (park / close)" if key == "end" else "{} nm".format(key)
            )
            box = QGroupBox(label)
            form = QFormLayout(box)
            for obj, spec in bp_objs.items():
                if spec["class"] in sc.SHUTTER_BEAMPATH_CLASSES:
                    cb = QCheckBox()
                    cb.setChecked(key != "end")
                    if key == "end":
                        cb.setEnabled(False)
                    self._bp_widgets[(key, obj)] = cb
                    form.addRow("{} (shutter)".format(obj), cb)
                else:
                    combo = QComboBox()
                    self._bp_widgets[(key, obj)] = combo
                    form.addRow("{} (preset)".format(obj), combo)
            self._bp_layout.addWidget(box)
        self._refill_presets()

    def _refill_presets(self, *args):
        if not self._cfg:
            return
        for (key, obj), w in self._bp_widgets.items():
            if not isinstance(w, QComboBox):
                continue
            group = self._group_combos.get(obj)
            presets = (
                self._cfg.groups.get(group.currentText(), []) if group else []
            )
            current = w.currentText()
            w.clear()
            w.addItems(presets)
            if current in presets:
                w.setCurrentText(current)

    def _beampath_answers(self):
        bp_objs = self._bp_objs()
        if not bp_objs or not self._cfg or not self._bp_widgets:
            return None
        beampath = {}
        for key in list(self._lasers()) + ["end"]:
            mapping = {}
            for obj in bp_objs:
                w = self._bp_widgets.get((key, obj))
                if isinstance(w, QCheckBox):
                    mapping[obj] = w.isChecked()
                elif isinstance(w, QComboBox):
                    mapping[obj] = w.currentText()
            beampath[key] = mapping
        return beampath

    def validatePage(self):
        wiz = self.wizard()
        if not self.author_cb.isChecked() or not self._lasers():
            wiz.protocol_entry = None
            return True
        try:
            laser_powers = {
                wl: sc.parse_number_list(e.text())
                for wl, e in self._power_edits.items()
            }
        except ValueError:
            self.info.setText(
                "Power set-points must be comma-separated numbers."
            )
            return False
        if not _confirm_overwrite(
            self, wiz.microscope_name, wiz.protocols_path, "protocol"
        ):
            self.info.setText(
                "A protocol named '{}' already exists; not overwriting.".format(
                    wiz.microscope_name
                )
            )
            return False
        wiz.protocol_name = wiz.microscope_name
        wiz.protocol_entry = sc.build_protocol_entry(
            {
                "laser_sequence": self._lasers(),
                "laser_powers": laser_powers,
                "beampath": self._beampath_answers(),
            }
        )
        return True


class _SummaryPage(QWizardPage):
    def __init__(self, env_file, parent=None):
        super().__init__(parent)
        self.env_file = env_file
        self.setTitle("Done")
        lay = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        lay.addWidget(self.summary)

    def initializePage(self):
        self.summary.setText(self.wizard().commit(self.env_file))


# ──────────────────────────────────────────────────────────────────────────
# Wizard
# ──────────────────────────────────────────────────────────────────────────
class SetupWizard(QWizard):
    """Guided onboarding wizard (GUI twin of ``monet setup``)."""

    def __init__(self, parent=None, env_file=None):
        super().__init__(parent)
        self.setWindowTitle("monet — setup")
        self.resize(640, 560)
        self.env_file = env_file or sc.default_env_path()
        # shared state the pages fill in
        self.updates = {}
        self.server_url = ""
        self.configs_path = ""
        self.protocols_path = ""
        self.microscope_name = None
        self.microscope_entry = None
        self.microscope_answers = None
        self.protocol_name = None
        self.protocol_entry = None

        state = sc.detect_state(probe_server=False)
        self.addPage(_IntroPage(state))
        self.addPage(_ServerPage())
        self.addPage(_CentralFolderPage())
        self.addPage(_MicroscopePage())
        self.addPage(_ProtocolPage())
        self.addPage(_SummaryPage(self.env_file))

    def commit(self, env_file):
        """Persist .env + (optional) microscope config; return a summary."""
        sc.write_env(self.updates, env_path=env_file)
        lines = ["Saved settings to {}".format(env_file)]
        if self.microscope_entry is not None and self.configs_path:
            sc.write_microscope_config(
                self.microscope_name, self.microscope_entry, self.configs_path
            )
            lines.append(
                "Wrote microscope '{}' to {}".format(
                    self.microscope_name, self.configs_path
                )
            )
        else:
            lines.append(
                "Add a microscope entry to {} (ONBOARDING Part 3).".format(
                    self.configs_path
                )
            )
        if self.protocol_entry is not None and self.protocols_path:
            sc.write_protocol_entry(
                self.protocol_name, self.protocol_entry, self.protocols_path
            )
            lines.append(
                "Wrote protocol '{}' to {}".format(
                    self.protocol_name, self.protocols_path
                )
            )
        # The config monet loaded at import does not change live, so the new
        # settings only take effect after a restart.
        lines.append(
            "\nRestart monet (close and reopen this application) for the new "
            "configuration to take effect."
        )
        return "\n".join(lines)


def maybe_run_setup(parent=None, force=False):
    """Open the wizard on first run (or when ``force``). Returns True if shown.

    Called by :class:`monet.gui.MonetMainWindow`: on first launch (no ``.env``
    and only built-in configs) the wizard opens automatically; the menu action
    passes ``force=True`` to re-run it anytime.
    """
    if not force:
        state = sc.detect_state(probe_server=False)
        if not state.first_run:
            return False
    wiz = SetupWizard(parent)
    wiz.exec()
    return True
