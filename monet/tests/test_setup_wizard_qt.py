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


if __name__ == "__main__":
    unittest.main()
