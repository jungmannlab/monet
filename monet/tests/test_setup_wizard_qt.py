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


if __name__ == "__main__":
    unittest.main()
