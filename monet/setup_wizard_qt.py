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
        self.rb_new = QRadioButton("Set up a new server")
        self.rb_existing.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.rb_existing, 0)
        group.addButton(self.rb_new, 1)
        lay.addWidget(self.rb_existing)
        lay.addWidget(self.rb_new)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_existing())
        self.stack.addWidget(self._build_new())
        lay.addWidget(self.stack)
        self.rb_existing.toggled.connect(
            lambda on: self.stack.setCurrentIndex(0 if on else 1)
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
        wiz.updates[sc.ENV_CONFIG_PATHS] = sc.paths_to_env([configs_path])
        wiz.updates[sc.ENV_PROTOCOL_PATHS] = sc.paths_to_env([protocols_path])
        return True


class _MicroscopePage(QWizardPage):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Microscope")
        lay = QVBoxLayout(self)
        self.author_cb = QCheckBox("Author a microscope entry now")
        self.author_cb.setChecked(True)
        lay.addWidget(self.author_cb)

        self.body = QWidget()
        form = QFormLayout(self.body)
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

        row = QHBoxLayout()
        btn_test = QPushButton("Test connection")
        btn_test.clicked.connect(self._test)
        row.addWidget(btn_test)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        row.addWidget(self.test_label, 1)
        form.addRow(row)
        form.addRow(
            QLabel(
                "Lasers / beam-path: add them in configs.yaml afterwards, or "
                "use `monet setup` on the CLI."
            )
        )
        lay.addWidget(self.body)
        self.author_cb.toggled.connect(self.body.setEnabled)

    def initializePage(self):
        if not self.db_edit.text().strip():
            url = getattr(self.wizard(), "server_url", "") or ""
            self.db_edit.setText(url)

    def _build_entry(self):
        return sc.build_microscope_entry(
            {
                "name": self.name_edit.text().strip() or "microscope",
                "database": self.db_edit.text().strip()
                or "./power_database.xlsx",
                "wavelength": self.wl_spin.value(),
                "laser_power": self.lp_spin.value(),
                "powermeter": self.pm_form.section(),
                "attenuation": self.at_form.section(),
                "analysis": self.an_form.section(),
            }
        )

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
            return True
        if not self.name_edit.text().strip():
            self.test_label.setText("A microscope name is required.")
            return False
        missing = (
            self.pm_form.missing_required()
            + self.at_form.missing_required()
            + self.an_form.missing_required()
        )
        if missing:
            self.test_label.setText(
                "Fill required fields: {}".format(", ".join(missing))
            )
            return False
        wiz.microscope_name = self.name_edit.text().strip()
        wiz.microscope_entry = self._build_entry()
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
        self.microscope_name = None
        self.microscope_entry = None

        state = sc.detect_state(probe_server=False)
        self.addPage(_IntroPage(state))
        self.addPage(_ServerPage())
        self.addPage(_CentralFolderPage())
        self.addPage(_MicroscopePage())
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
