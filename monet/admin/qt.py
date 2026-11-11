"""``monet-admin-gui`` — PyQt6 admin console over :mod:`.core`.

A small desktop app that SSHes to the deployed monet server and drives the
server-side ``monet-admin`` wrapper: connection panel plus Status, Tokens,
Logs, and Server (restart/upgrade) tabs. All SSH work runs off the UI thread in
a :class:`QThread` worker whose ``output`` signal is wired to the core's
``on_output`` seam (mirrors monet's setup wizard).

Runs from an admin workstation (a laptop) — it needs no monet server or
hardware locally, only the ``[admin]`` extra (PyQt6 + paramiko). It can be
launched standalone (``monet-admin-gui``) or opened from the monet GUI's
Setup menu (:class:`AdminWindow` takes a parent).
"""

from __future__ import annotations

import sys
import traceback

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .core import MonetAdmin
from .profile import Profile, load_profile, save_profile

#: Prefill for the upgrade field.
_IMAGE_PREFIX = "ghcr.io/jungmannlab/monet:"


class _Worker(QThread):
    """Runs one ``fn(on_output) -> result`` off the UI thread."""

    output = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        # fn(on_output) -> result; on_output(str) streams live.
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # noqa: D401 - QThread entry
        try:
            result = self._fn(self.output.emit)
        except Exception as exc:  # surface any failure as a signal
            self.failed.emit(str(exc))
            return
        self.done.emit(result)


class _AddTokenDialog(QDialog):
    """Collect (scope, label) for a new token."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Mint token")
        form = QFormLayout(self)
        self.scope = QComboBox()
        self.scope.addItems(["write", "read"])
        self.label = QLineEdit()
        self.label.setPlaceholderText("holder, e.g. microscope-mercury")
        form.addRow("Scope", self.scope)
        form.addRow("Label", self.label)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str]:
        return self.scope.currentText(), self.label.text().strip()


class AdminWindow(QMainWindow):
    """Standalone admin console window (also embeddable via a parent)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("monet admin")
        self._worker: _Worker | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.addWidget(self._build_connection_panel())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_status_tab(), "Status")
        self.tabs.addTab(self._build_tokens_tab(), "Tokens")
        self.tabs.addTab(self._build_logs_tab(), "Logs")
        self.tabs.addTab(self._build_server_tab(), "Server")
        root.addWidget(self.tabs, 1)

        root.addWidget(QLabel("Activity"))
        self.activity = QPlainTextEdit(readOnly=True)
        self.activity.setMaximumBlockCount(5000)
        root.addWidget(self.activity, 1)

        self._load_into_form(load_profile())
        self.resize(760, 640)

    # -- connection panel -------------------------------------------------
    def _build_connection_panel(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        self.host = QLineEdit()
        self.user = QLineEdit()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.identity = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_identity)
        key_row = QHBoxLayout()
        key_row.addWidget(self.identity, 1)
        key_row.addWidget(browse)
        key_w = QWidget()
        key_w.setLayout(key_row)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("(blank = key/agent auth)")
        self.use_sudo = QCheckBox("Run wrapper via sudo")
        self.conn_status = QLabel("not connected")

        form.addRow("Host", self.host)
        form.addRow("User", self.user)
        form.addRow("SSH port", self.port)
        form.addRow("Identity file", key_w)
        form.addRow("Password", self.password)
        form.addRow("", self.use_sudo)
        test = QPushButton("Test connection")
        test.clicked.connect(self._on_test)
        form.addRow(test, self.conn_status)
        return box

    def _pick_identity(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "SSH private key")
        if path:
            self.identity.setText(path)

    # -- status tab -------------------------------------------------------
    def _build_status_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        refresh = QPushButton("Refresh status")
        refresh.clicked.connect(self._on_test)
        lay.addWidget(refresh)
        self.status_view = QPlainTextEdit(readOnly=True)
        lay.addWidget(self.status_view, 1)
        return w

    # -- tokens tab -------------------------------------------------------
    def _build_tokens_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        for text, slot in [
            ("Refresh", self._refresh_tokens),
            ("Add…", self._add_token),
            ("Rotate", self._rotate_token),
            ("Revoke", self._revoke_token),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.token_table = QTableWidget(0, 2)
        self.token_table.setHorizontalHeaderLabels(["Scope", "Label"])
        self.token_table.horizontalHeader().setStretchLastSection(True)
        self.token_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.token_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        lay.addWidget(self.token_table, 1)
        return w

    # -- logs tab ---------------------------------------------------------
    def _build_logs_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Lines"))
        self.log_lines = QSpinBox()
        self.log_lines.setRange(10, 10000)
        self.log_lines.setValue(200)
        bar.addWidget(self.log_lines)
        tail = QPushButton("Tail")
        tail.clicked.connect(self._tail_logs)
        bar.addWidget(tail)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(20000)
        lay.addWidget(self.log_view, 1)
        return w

    # -- server tab -------------------------------------------------------
    def _build_server_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        restart = QPushButton("Restart container")
        restart.clicked.connect(self._restart)
        lay.addWidget(restart)
        lay.addSpacing(12)
        lay.addWidget(QLabel("Upgrade to image"))
        row = QHBoxLayout()
        self.upgrade_ref = QLineEdit(_IMAGE_PREFIX)
        row.addWidget(self.upgrade_ref, 1)
        up = QPushButton("Upgrade")
        up.clicked.connect(self._upgrade)
        row.addWidget(up)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    # -- profile <-> form -------------------------------------------------
    def _load_into_form(self, p: Profile) -> None:
        self.host.setText(p.host)
        self.user.setText(p.user)
        self.port.setValue(p.port or 22)
        self.identity.setText(p.identity)
        self.use_sudo.setChecked(p.use_sudo)

    def _current_profile(self) -> Profile:
        return Profile(
            host=self.host.text().strip(),
            user=self.user.text().strip(),
            port=self.port.value(),
            identity=self.identity.text().strip(),
            use_sudo=self.use_sudo.isChecked(),
        )

    # -- the async runner (single worker, re-entry guarded) ---------------
    def _run(self, action, on_done, *, busy: str) -> None:
        """Run ``action(admin) -> result`` off the UI thread.

        ALL connection inputs are snapshotted here, on the GUI thread; the
        worker closure only touches the plain snapshot + the built
        ``MonetAdmin``, never a Qt widget (widgets aren't thread-safe).
        """
        if self._worker is not None and self._worker.isRunning():
            self.activity.appendPlainText("(busy — wait for the current task)")
            return
        profile = self._current_profile()
        password = self.password.text() or None
        if not profile.host or not profile.user:
            QMessageBox.information(
                self, "Connect", "Set a host and user first."
            )
            return
        save_profile(profile)
        self.activity.appendPlainText(f"• {busy}")
        self.setCursor(Qt.CursorShape.WaitCursor)

        def fn(on_output):
            admin = MonetAdmin(
                profile.host,
                profile.user,
                password=password,
                key_filename=profile.identity or None,
                port=profile.port,
                use_sudo=profile.use_sudo,
                on_output=on_output,
            )
            with admin:
                return action(admin)

        worker = _Worker(fn, self)
        worker.output.connect(self._append_activity)
        worker.done.connect(on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda w=worker: self._on_finished(w))
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _append_activity(self, text: str) -> None:
        self.activity.moveCursor(QTextCursor.MoveOperation.End)
        self.activity.insertPlainText(text)

    def _on_failed(self, msg: str) -> None:
        self.conn_status.setText("error")
        self.activity.appendPlainText(f"✗ {msg}")
        QMessageBox.critical(self, "Operation failed", msg)

    def _on_finished(self, worker) -> None:
        self.unsetCursor()
        # Only clear if this is STILL the current worker: a done-slot may have
        # started a new worker before this (older) one's finished fired, and we
        # must not drop the new worker's reference.
        if self._worker is worker:
            self._worker = None

    # -- actions ----------------------------------------------------------
    def _on_test(self) -> None:
        def done(status):
            mark = "running" if status.running else "DOWN"
            health = "ok" if status.health_ok else "unreachable"
            ver = f" · {status.version}" if status.version else ""
            self.conn_status.setText(f"{mark} · health {health}{ver}")
            lines = [f"{k}: {v}" for k, v in status.raw.items()]
            self.status_view.setPlainText("\n".join(lines))

        self._run(lambda a: a.status(), done, busy="checking status")

    def _refresh_tokens(self) -> None:
        def done(entries):
            self.token_table.setRowCount(len(entries))
            for r, e in enumerate(entries):
                self.token_table.setItem(r, 0, QTableWidgetItem(e.scope))
                self.token_table.setItem(r, 1, QTableWidgetItem(e.label))

        self._run(lambda a: a.token_list(), done, busy="listing tokens")

    def _selected_label(self) -> str | None:
        row = self.token_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Tokens", "Select a token first.")
            return None
        item = self.token_table.item(row, 1)
        return item.text() if item else None

    def _add_token(self) -> None:
        dlg = _AddTokenDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        scope, label = dlg.values()
        if not label:
            return
        self._run(
            lambda a: a.token_add(scope, label),
            self._after_mint,
            busy=f"minting {scope} token {label}",
        )

    def _rotate_token(self) -> None:
        label = self._selected_label()
        if not label:
            return
        if (
            QMessageBox.question(
                self, "Rotate", f"Rotate the token for {label!r}?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run(
            lambda a: a.token_rotate(label),
            self._after_mint,
            busy=f"rotating {label}",
        )

    def _revoke_token(self) -> None:
        label = self._selected_label()
        if not label:
            return
        if (
            QMessageBox.question(
                self, "Revoke", f"Revoke the token for {label!r}?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run(
            lambda a: a.token_revoke(label),
            lambda _: self._refresh_tokens(),
            busy=f"revoking {label}",
        )

    def _after_mint(self, output: str) -> None:
        from .core import parse_minted_token

        self._refresh_tokens()
        value = parse_minted_token(output)
        if value:
            box = QMessageBox(self)
            box.setWindowTitle("Token minted")
            box.setText("Copy this value now — it is shown once.\n\n" + value)
            copy = box.addButton("Copy", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() is copy:
                QApplication.clipboard().setText(value)
        else:
            QMessageBox.information(self, "Token minted", output.strip())

    def _tail_logs(self) -> None:
        n = self.log_lines.value()

        def done(text):
            self.log_view.setPlainText(text)
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)

        self._run(lambda a: a.logs(n), done, busy=f"tailing {n} log lines")

    def _restart(self) -> None:
        if (
            QMessageBox.question(
                self, "Restart", "Restart the monet server container?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run(
            lambda a: a.restart(),
            lambda _: self._on_test(),
            busy="restarting",
        )

    def _upgrade(self) -> None:
        ref = self.upgrade_ref.text().strip()
        if not ref or ref.endswith(":"):
            QMessageBox.information(
                self, "Upgrade", "Enter a full image ref (…:VERSION)."
            )
            return
        if (
            QMessageBox.question(self, "Upgrade", f"Pull and deploy {ref}?")
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._run(
            lambda a: a.upgrade(ref),
            lambda _: self._on_test(),
            busy=f"upgrading to {ref}",
        )


def _install_excepthook() -> None:
    """Show unhandled exceptions in a dialog instead of killing the process."""
    if getattr(sys, "_monet_admin_hook", False):
        return

    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            # A modal dialog is only safe from the GUI thread with a live app;
            # an exception on a secondary thread / during teardown just logs.
            app = QApplication.instance()
            if app is not None and QThread.currentThread() == app.thread():
                QMessageBox.critical(None, "Unexpected error", msg)
        finally:
            sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
    sys._monet_admin_hook = True  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    _install_excepthook()
    window = AdminWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
