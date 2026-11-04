"""
monet/tests/test_setup_wizard_qt.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Smoke tests for the PyQt6 setup wizard. Skipped automatically when PyQt6 is not
installed; drives the pages programmatically (no user interaction) and asserts
the wizard commits a .env + configs.yaml.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import pytest
import yaml

pytest.importorskip("PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from monet import setup_core as sc  # noqa: E402
from monet import setup_wizard_qt as swq  # noqa: E402

_app = QApplication.instance() or QApplication([])


class TestSetupWizard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env_file = os.path.join(self.tmp, ".env")
        self._orig = dict(os.environ)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._orig)

    def test_driver_form_builds_section(self):
        form = swq._DriverForm("powermeter")
        section = form.section()
        self.assertEqual(section["class"], "TestPowerMeter")
        # defaults flow through into init_kwargs
        self.assertEqual(section["init_kwargs"]["address"], "find connection")
        self.assertEqual(section["init_kwargs"]["amp"], 50.0)

    def test_driver_form_required_detection(self):
        form = swq._DriverForm("attenuation")
        # pick KinesisAttenuator (has a required 'serial')
        idx = form.combo.findData("KinesisAttenuator")
        form.combo.setCurrentIndex(idx)
        self.assertEqual(form.missing_required(), ["serial"])

    def test_wizard_commit_writes_env_and_config(self):
        wiz = swq.SetupWizard(env_file=self.env_file)
        configs_path = os.path.join(self.tmp, "configs.yaml")
        # Populate the shared state as the pages would.
        wiz.server_url = "http://localhost:8000"
        wiz.configs_path = configs_path
        wiz.updates = {
            sc.ENV_CONFIG_PATHS: sc.paths_to_env([configs_path]),
        }
        wiz.microscope_name = "wiz_sim"
        wiz.microscope_entry = sc.build_microscope_entry(
            {
                "name": "wiz_sim",
                "database": "http://localhost:8000",
                "wavelength": 488,
                "laser_power": 100,
                "powermeter": {
                    "class": "TestPowerMeter",
                    "init_kwargs": {
                        "address": "find connection",
                        "bkg": 0,
                        "amp": 50,
                        "phi": 30,
                        "start": 10,
                        "step": 5,
                        "noise": 0.5,
                    },
                },
                "attenuation": {
                    "class": "TestAttenuator",
                    "init_kwargs": {},
                },
                "analysis": {
                    "class": "SinusAttenuationCurveAnalyzer",
                    "init_kwargs": {"min": 0, "max": 180, "step": 5},
                },
            }
        )

        summary = wiz.commit(self.env_file)
        self.assertIn("wiz_sim", summary)

        env = sc.read_env(self.env_file)
        self.assertIn(sc.ENV_CONFIG_PATHS, env)
        # The authored microscope is recorded as the GUI's default microscope.
        self.assertEqual(env[sc.ENV_MICROSCOPE], "wiz_sim")
        with open(configs_path) as f:
            configs = yaml.full_load(f)
        self.assertIn("wiz_sim", configs)
        self.assertEqual(
            configs["wiz_sim"]["powermeter"]["classpath"],
            "monet.powermeter.TestPowerMeter",
        )

    def test_server_page_validate_sets_url(self):
        wiz = swq.SetupWizard(env_file=self.env_file)
        page = wiz.page(1)  # _ServerPage
        page.rb_existing.setChecked(True)
        page.url_edit.setText("http://host:8000/")
        self.assertTrue(page.validatePage())
        self.assertEqual(wiz.server_url, "http://host:8000")

    def test_server_page_local_no_server(self):
        wiz = swq.SetupWizard(env_file=self.env_file)
        page = wiz.page(1)  # _ServerPage
        page.rb_local.setChecked(True)
        self.assertTrue(page.validatePage())
        self.assertEqual(wiz.server_url, "")
        self.assertNotIn(sc.ENV_CLIENT_TOKEN, wiz.updates)

    def test_existing_server_mint_fills_token(self):
        # The existing-server page can mint this rig's token over SSH and drop
        # it into the token field (host taken from the URL).
        page = swq._ServerPage()
        page.url_edit.setText("http://host:8000")
        page.ex_ssh_user.setText("lab")
        page.ex_ssh_pw.setText("pw")

        captured = {}

        class _FakeMintWorker(swq.QThread):
            output = swq.pyqtSignal(str)
            done = swq.pyqtSignal(str)
            failed = swq.pyqtSignal(str)

            def __init__(self, kwargs, parent=None):
                super().__init__(parent)
                captured["kwargs"] = kwargs

            def isRunning(self):
                return False

            def start(self):
                self.done.emit("minted-xyz")

        with mock.patch.object(swq, "_SshMintWorker", _FakeMintWorker):
            page._mint_existing()

        self.assertEqual(page.token_edit.text(), "minted-xyz")
        self.assertEqual(captured["kwargs"]["host"], "host")
        self.assertEqual(captured["kwargs"]["user"], "lab")
        self.assertEqual(captured["kwargs"]["password"], "pw")
        self.assertEqual(captured["kwargs"]["rig"], "acquisition-rig")

    def test_existing_server_mint_needs_url_and_user(self):
        # No URL / SSH user yet -> guarded: no worker runs, token stays empty.
        page = swq._ServerPage()
        page._mint_existing()
        self.assertEqual(page.token_edit.text(), "")

    def test_update_server_dialog_prefills_and_runs(self):
        # The update-server dialog prefills host/port from the URL and runs the
        # in-place update over SSH (no token minted).
        dlg = swq.UpdateServerDialog(server_url="http://srv.lab:9001")
        self.assertEqual(dlg.host_edit.text(), "srv.lab")
        self.assertEqual(dlg.port_spin.value(), 9001)
        dlg.ssh_user.setText("lab")
        dlg.ssh_pw.setText("pw")

        captured = {}

        class _FakeUpdateWorker(swq.QThread):
            output = swq.pyqtSignal(str)
            done = swq.pyqtSignal(object)
            failed = swq.pyqtSignal(str)

            def __init__(self, kwargs, parent=None):
                super().__init__(parent)
                captured["kwargs"] = kwargs

            def isRunning(self):
                return False

            def start(self):
                self.done.emit({"url": "http://srv.lab:9001"})

        with mock.patch.object(swq, "_SshUpdateWorker", _FakeUpdateWorker):
            dlg._update()

        self.assertEqual(captured["kwargs"]["host"], "srv.lab")
        self.assertEqual(captured["kwargs"]["user"], "lab")
        self.assertEqual(captured["kwargs"]["port"], 9001)
        self.assertTrue(dlg.btn_update.isEnabled())

    def test_update_server_dialog_needs_host_and_user(self):
        dlg = swq.UpdateServerDialog(server_url=None)
        dlg._update()  # nothing entered -> guarded, button stays enabled
        self.assertTrue(dlg.btn_update.isEnabled())

    def test_protocol_page_authors_from_cfg(self):
        cfg_path = os.path.join(self.tmp, "scope.cfg")
        with open(cfg_path, "w") as f:
            f.write(
                "ConfigGroup,Filter turret,Ti488setting,FW,Label,1\n"
                "ConfigGroup,Filter turret,Ti561setting,FW,Label,2\n"
                "Property,Core,Shutter,MyShutter\n"
            )
        protocols_path = os.path.join(self.tmp, "protocols.yaml")
        configs_path = os.path.join(self.tmp, "configs.yaml")

        wiz = swq.SetupWizard(env_file=self.env_file)
        answers = {
            "name": "rig",
            "database": "./x.xlsx",
            "wavelength": 488,
            "laser_power": 100,
            "powermeter": {"class": "TestPowerMeter", "init_kwargs": {}},
            "attenuation": {"class": "TestAttenuator", "init_kwargs": {}},
            "analysis": {
                "class": "SinusAttenuationCurveAnalyzer",
                "init_kwargs": {"min": 0, "max": 180, "step": 5},
            },
            "lasers": {
                488: {"class": "TestLaser", "init_kwargs": {}},
                561: {"class": "TestLaser", "init_kwargs": {}},
            },
            "beampath": {
                "DC": {"class": "NikonFilterWheel", "init_kwargs": {}},
                "shutter": {"class": "NikonShutter", "init_kwargs": {}},
            },
        }
        wiz.microscope_name = "rig"
        wiz.microscope_answers = answers
        wiz.microscope_entry = sc.build_microscope_entry(answers)
        wiz.configs_path = configs_path
        wiz.protocols_path = protocols_path
        wiz.updates = {}

        page = wiz.page(4)  # _ProtocolPage
        page.initializePage()
        page._power_edits[488].setText("50,100")
        page._power_edits[561].setText("100,200")
        page.cfg_edit.setText(cfg_path)
        page._load_cfg()
        page._bp_widgets[(488, "DC")].setCurrentText("Ti488setting")
        page._bp_widgets[(561, "DC")].setCurrentText("Ti561setting")
        page._bp_widgets[("end", "DC")].setCurrentText("Ti488setting")
        self.assertTrue(page.validatePage())

        wiz.commit(self.env_file)
        with open(protocols_path) as f:
            proto = yaml.full_load(f)["rig"]
        self.assertEqual(proto["laser_sequence"], [488, 561])
        self.assertEqual(proto["laser_powers"][561], [100, 200])
        self.assertEqual(
            proto["beampath"][488], {"DC": "Ti488setting", "shutter": True}
        )
        self.assertEqual(
            proto["beampath"]["end"], {"DC": "Ti488setting", "shutter": False}
        )


class TestPickExistingMicroscope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "configs.yaml")
        with open(self.cfg, "w") as f:
            yaml.dump(
                {
                    "Stokes": {"database": "http://s:8000"},
                    "Voyager": {"database": "./v.xlsx"},
                },
                f,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _microscope_page(self, wiz):
        for i in wiz.pageIds():
            if wiz.page(i).title() == "Microscope":
                return wiz.page(i)
        raise AssertionError("no microscope page")

    def test_existing_entries_listed_and_selectable(self):
        wiz = swq.SetupWizard()
        wiz.configs_path = self.cfg
        page = self._microscope_page(wiz)
        page.initializePage()
        names = [
            page.existing_combo.itemText(i)
            for i in range(page.existing_combo.count())
        ]
        self.assertIn("Stokes", names)
        self.assertIn("Voyager", names)

    def test_picking_existing_sets_default_without_authoring(self):
        wiz = swq.SetupWizard()
        wiz.configs_path = self.cfg
        page = self._microscope_page(wiz)
        page.initializePage()
        page.existing_combo.setCurrentIndex(
            page.existing_combo.findData("Stokes")
        )
        # author section greys out; validate reuses the entry as-is
        self.assertFalse(page.body.isEnabled())
        self.assertTrue(page.validatePage())
        self.assertEqual(wiz.microscope_name, "Stokes")
        self.assertIsNone(wiz.microscope_entry)  # not re-authored
        self.assertIsNone(wiz.microscope_answers)

    def test_commit_sets_default_microscope_for_existing(self):
        wiz = swq.SetupWizard()
        wiz.configs_path = self.cfg
        wiz.microscope_name = "Stokes"
        wiz.microscope_entry = None
        env_file = os.path.join(self.tmp, ".env")
        summary = wiz.commit(env_file)
        self.assertIn("Using existing microscope 'Stokes'", summary)
        env = sc.read_env(env_file)
        self.assertEqual(env[sc.ENV_MICROSCOPE], "Stokes")

    def test_commit_repoints_existing_local_db_to_server(self):
        # Picked an existing microscope whose DB is a local .xlsx, with a server
        # configured -> re-point it to the server (so the GUI reads the migrated
        # calibrations from there, not the stale local file).
        wiz = swq.SetupWizard()
        wiz.configs_path = self.cfg
        wiz.server_url = "http://srv:8000"
        wiz.microscope_name = "Voyager"  # database "./v.xlsx"
        wiz.microscope_entry = None
        summary = wiz.commit(os.path.join(self.tmp, ".env"))
        self.assertIn("Re-pointed", summary)
        with open(self.cfg) as f:
            data = yaml.full_load(f)
        self.assertEqual(data["Voyager"]["database"], "http://srv:8000")

    def test_commit_does_not_repoint_entry_already_on_server(self):
        wiz = swq.SetupWizard()
        wiz.configs_path = self.cfg
        wiz.server_url = "http://srv:8000"
        wiz.microscope_name = "Stokes"  # already "http://s:8000"
        wiz.microscope_entry = None
        summary = wiz.commit(os.path.join(self.tmp, ".env"))
        self.assertIn("configs.yaml unchanged", summary)
        with open(self.cfg) as f:
            data = yaml.full_load(f)
        self.assertEqual(data["Stokes"]["database"], "http://s:8000")


class TestEditConfigDialog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "configs.yaml")
        self.proto = os.path.join(self.tmp, "protocols.yaml")
        with open(self.cfg, "w") as f:
            yaml.dump(
                {
                    "Stokes": {
                        "database": "./s.xlsx",
                        "index": {
                            "name": "Stokes",
                            "wavelength [nm]": 561,
                            "laser_power [mW]": 200,
                        },
                        "powermeter": {
                            "classpath": "monet.powermeter.TestPowerMeter",
                            "init_kwargs": {"amp": 50},
                        },
                        "attenuation": {
                            "classpath": "monet.attenuation.TestAttenuator",
                            "init_kwargs": {},
                        },
                        "analysis": {
                            "classpath": "monet.analysis."
                            "SinusAttenuationCurveAnalyzer",
                            "init_kwargs": {},
                        },
                        "lasers": {
                            488: {
                                "classpath": "monet.laser.TestLaser",
                                "init_kwargs": {},
                            }
                        },
                    }
                },
                f,
            )
        with open(self.proto, "w") as f:
            yaml.dump(
                {
                    "Stokes": {
                        "laser_sequence": [488],
                        "laser_powers": {488: [100, 200]},
                    }
                },
                f,
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_current_microscope_is_preselected(self):
        data = yaml.full_load(open(self.cfg))
        data["Aaa"] = {"database": "./a.xlsx", "index": {"name": "Aaa"}}
        with open(self.cfg, "w") as f:
            yaml.dump(data, f)
        d = swq.EditConfigDialog(
            configs_path=self.cfg,
            protocols_path=self.proto,
            current="Stokes",
        )
        # "Aaa" sorts first, but current= pre-selects Stokes.
        self.assertEqual(d._combo.currentData(), "Stokes")

    def test_loads_saved_values(self):
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        self.assertEqual(d._db_edit.text(), "./s.xlsx")
        self.assertEqual(d._wl_spin.value(), 561)
        self.assertEqual(d._pm_form.current_name(), "TestPowerMeter")
        self.assertTrue(d._pm_form.loaded_ok())
        self.assertIn(488, d._power_edits)

    def test_save_edits_and_preserves_untouched_sections(self):
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        d._db_edit.setText("http://srv:8000")
        d._power_edits[488].setText("50, 150, 300")
        d._save()
        with open(self.cfg) as f:
            entry = yaml.full_load(f)["Stokes"]
        self.assertEqual(entry["database"], "http://srv:8000")
        self.assertIn("lasers", entry)  # preserved, not edited away
        with open(self.proto) as f:
            self.assertEqual(
                yaml.full_load(f)["Stokes"]["laser_powers"][488],
                [50, 150, 300],
            )

    def test_absent_section_is_not_added_on_save(self):
        # A config with no 'analysis' section must not gain a default one.
        data = yaml.full_load(open(self.cfg))
        del data["Stokes"]["analysis"]
        with open(self.cfg, "w") as f:
            yaml.dump(data, f)
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        d._db_edit.setText("http://srv:8000")
        d._save()
        with open(self.cfg) as f:
            entry = yaml.full_load(f)["Stokes"]
        self.assertNotIn("analysis", entry)
        self.assertEqual(entry["database"], "http://srv:8000")

    def test_preserves_unmodeled_init_kwarg_on_unchanged_driver(self):
        data = yaml.full_load(open(self.cfg))
        data["Stokes"]["powermeter"]["init_kwargs"]["legacy"] = 7
        with open(self.cfg, "w") as f:
            yaml.dump(data, f)
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        d._save()  # driver unchanged
        with open(self.cfg) as f:
            kw = yaml.full_load(f)["Stokes"]["powermeter"]["init_kwargs"]
        self.assertEqual(kw.get("legacy"), 7)

    def test_clearing_a_modeled_kwarg_removes_it_keeps_unmodeled(self):
        data = yaml.full_load(open(self.cfg))
        data["Stokes"]["attenuation"] = {
            "classpath": "monet.attenuation.KinesisAttenuator",
            "init_kwargs": {"serial": "27257033", "legacy_extra": 9},
        }
        with open(self.cfg, "w") as f:
            yaml.dump(data, f)
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        self.assertEqual(d._at_form.current_name(), "KinesisAttenuator")
        for fname, (f, edit) in d._at_form._widgets.items():
            if fname == "serial":
                edit.setText("")  # user clears the serial
        d._save()
        with open(self.cfg) as fh:
            kw = yaml.full_load(fh)["Stokes"]["attenuation"]["init_kwargs"]
        self.assertNotIn("serial", kw)  # cleared modeled field removed
        self.assertEqual(kw.get("legacy_extra"), 9)  # unmodeled key preserved

    def test_absent_section_form_is_disabled(self):
        data = yaml.full_load(open(self.cfg))
        del data["Stokes"]["analysis"]
        with open(self.cfg, "w") as f:
            yaml.dump(data, f)
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        self.assertFalse(d._an_form.isEnabled())  # absent -> disabled
        self.assertTrue(d._pm_form.isEnabled())  # present -> editable

    def test_bad_power_input_writes_nothing(self):
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        d._db_edit.setText("http://CHANGED:8000")
        d._power_edits[488].setText("10, 2o")  # invalid token
        d._save()
        with open(self.cfg) as f:
            entry = yaml.full_load(f)["Stokes"]
        self.assertEqual(entry["database"], "./s.xlsx")  # NOT written
        self.assertIn("Invalid power", d._status.text())

    def test_unknown_driver_is_preserved_on_save(self):
        # A driver not in the registry must survive an edit+save unchanged.
        import yaml as _yaml

        data = _yaml.full_load(open(self.cfg))
        data["Stokes"]["powermeter"] = {
            "classpath": "some.custom.Meter",
            "init_kwargs": {"x": 1},
        }
        with open(self.cfg, "w") as f:
            _yaml.dump(data, f)
        d = swq.EditConfigDialog(
            configs_path=self.cfg, protocols_path=self.proto
        )
        self.assertFalse(d._pm_form.loaded_ok())
        d._save()
        with open(self.cfg) as f:
            pm = yaml.full_load(f)["Stokes"]["powermeter"]
        self.assertEqual(pm["classpath"], "some.custom.Meter")
        self.assertEqual(pm["init_kwargs"], {"x": 1})


class TestPasswordReveal(unittest.TestCase):
    def test_reveal_button_toggles_echo_mode(self):
        from PyQt6.QtWidgets import QLineEdit, QToolButton

        le = QLineEdit()
        le.setEchoMode(QLineEdit.EchoMode.Password)
        host = swq._with_reveal(le)
        btn = host.findChild(QToolButton)
        self.assertIsNotNone(btn)
        self.assertEqual(le.echoMode(), QLineEdit.EchoMode.Password)
        btn.setChecked(True)
        self.assertEqual(le.echoMode(), QLineEdit.EchoMode.Normal)
        btn.setChecked(False)
        self.assertEqual(le.echoMode(), QLineEdit.EchoMode.Password)

    def test_server_page_password_fields_have_reveal(self):
        from PyQt6.QtWidgets import QToolButton

        page = swq._ServerPage()
        # the SSH password + token fields each got an eye toggle
        for field in (page.ssh_pw, page.new_token, page.token_edit):
            container = field.parentWidget()
            self.assertIsNotNone(container.findChild(QToolButton))


class TestMigrateUI(unittest.TestCase):
    def test_wizard_includes_import_page(self):
        wiz = swq.SetupWizard()
        titles = [wiz.page(i).title() for i in wiz.pageIds()]
        self.assertTrue(
            any("Import existing data" in t for t in titles), titles
        )

    def test_migrate_page_body_follows_checkbox(self):
        page = swq._MigratePage()
        self.assertFalse(page.body.isEnabled())
        page.enable_cb.setChecked(True)
        self.assertTrue(page.body.isEnabled())

    def test_migrate_dialog_prefills_server(self):
        dlg = swq.MigrateDialog(default_server="http://srv:8000")
        self.assertEqual(dlg._url_edit.text(), "http://srv:8000")

    def test_migrate_dialog_rejects_missing_inputs(self):
        dlg = swq.MigrateDialog()
        # no file, no url -> validation returns None and does not start a worker
        self.assertIsNone(dlg._inputs())
        dlg._start(dry_run=True)
        self.assertIsNone(dlg._worker)

    def test_migrate_worker_emits_done(self):
        # Drive the worker synchronously by patching the engine.
        from unittest import mock

        with mock.patch(
            "monet.migrate.migrate_excel_to_server", return_value=(3, 1)
        ):
            worker = swq.MigrateWorker("x.xlsx", "http://srv:8000")
            results = []
            worker.done.connect(lambda i, s: results.append((i, s)))
            worker.run()  # run() directly (no thread) for a deterministic test
        self.assertEqual(results, [(3, 1)])


if __name__ == "__main__":
    unittest.main()
