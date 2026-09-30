"""
monet/tests/test_config_persist.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for persisting a microscope's analysis-model config back to the
config file (monet.set_config_analysis), used by the GUI "Apply best model".
"""

import os
import tempfile
import unittest

import yaml

import monet


class TestSetConfigAnalysis(unittest.TestCase):

    def setUp(self):
        self._orig_configs = monet.CONFIGS
        self._orig_path = monet.CONFIGS_PATH
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "configs.yaml")
        self.configs = {
            "scopeX": {
                "index": {"name": "scopeX"},
                "analysis": {
                    "classpath": "monet.analysis."
                    "SinusAttenuationCurveAnalyzer",
                    "init_kwargs": {"min": 30, "max": 130, "step": 5},
                },
            }
        }
        with open(self.path, "w") as f:
            yaml.dump(self.configs, f)
        monet.CONFIGS = self.configs
        monet.CONFIGS_PATH = self.path

    def tearDown(self):
        monet.CONFIGS = self._orig_configs
        monet.CONFIGS_PATH = self._orig_path

    def test_persists_and_backs_up(self):
        new_ana = {
            "classpath": "monet.analysis.PolynomAttenuationCurveAnalyzer",
            "init_kwargs": {"min": 30, "max": 130, "step": 5, "polydegree": 5},
        }
        written = monet.set_config_analysis("scopeX", new_ana)
        self.assertEqual(written, self.path)
        # backup of the previous file exists
        self.assertTrue(os.path.isfile(self.path + ".bak"))
        # in-memory updated
        self.assertEqual(monet.CONFIGS["scopeX"]["analysis"], new_ana)
        # file on disk reflects the new model
        with open(self.path) as f:
            reloaded = yaml.full_load(f)
        self.assertEqual(
            reloaded["scopeX"]["analysis"]["classpath"],
            "monet.analysis.PolynomAttenuationCurveAnalyzer",
        )
        self.assertEqual(
            reloaded["scopeX"]["analysis"]["init_kwargs"]["polydegree"], 5
        )
        # other microscopes / sections are preserved
        self.assertEqual(reloaded["scopeX"]["index"]["name"], "scopeX")

    def test_unknown_microscope_returns_none(self):
        self.assertIsNone(monet.set_config_analysis("nope", {"a": 1}))

    def test_no_config_file_updates_memory_only(self):
        monet.CONFIGS_PATH = ""
        new_ana = {"classpath": "x", "init_kwargs": {}}
        self.assertIsNone(monet.set_config_analysis("scopeX", new_ana))
        # still updated in memory
        self.assertEqual(monet.CONFIGS["scopeX"]["analysis"], new_ana)


if __name__ == "__main__":
    unittest.main()
