"""
monet/tests/test_setup_core.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The headless engine behind ``monet setup``: .env read/write (0600), the driver
registry, microscope-config authoring + persistence, template scaffolding, the
no-actuation connection check against ``Test*`` hardware, and the server-install
helpers.
"""

import importlib
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from monet import setup_core as sc


def _sim_answers(name="wiz_sim", database="./wiz_power.xlsx"):
    return {
        "name": name,
        "database": database,
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
            "init_kwargs": {
                "bkg": 0,
                "amp": 50,
                "phi": 30,
                "start": 10,
                "step": 5,
            },
        },
        "analysis": {
            "class": "SinusAttenuationCurveAnalyzer",
            "init_kwargs": {"min": 30, "max": 100, "step": 5},
        },
        "lasers": {488: {"class": "TestLaser", "init_kwargs": {}}},
    }


class TestEnvIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = os.path.join(self.tmp, "sub", ".env")
        self._orig = dict(os.environ)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._orig)

    def test_write_read_roundtrip_and_mode(self):
        sc.write_env(
            {
                sc.ENV_CONFIG_PATHS: "/a/configs.yaml",
                sc.ENV_CLIENT_TOKEN: "secret-tok",
                "SKIP_ME": None,
            },
            env_path=self.env,
        )
        data = sc.read_env(self.env)
        self.assertEqual(data[sc.ENV_CONFIG_PATHS], "/a/configs.yaml")
        self.assertEqual(data[sc.ENV_CLIENT_TOKEN], "secret-tok")
        self.assertNotIn("SKIP_ME", data)  # None values are skipped
        # live env reflects the write
        self.assertEqual(os.environ[sc.ENV_CLIENT_TOKEN], "secret-tok")
        # the secret file is owner-only
        mode = stat.S_IMODE(os.stat(self.env).st_mode)
        self.assertEqual(mode, 0o600)

    def test_read_missing_returns_empty(self):
        self.assertEqual(sc.read_env(self.env), {})

    def test_paths_to_env(self):
        joined = sc.paths_to_env(["/a/configs.yaml", "", "/b/configs.yaml"])
        self.assertEqual(
            joined, os.pathsep.join(["/a/configs.yaml", "/b/configs.yaml"])
        )


class TestDriverRegistry(unittest.TestCase):
    def test_every_classpath_imports(self):
        for category, drivers in sc.DRIVERS.items():
            for name, drv in drivers.items():
                module, cls = drv.classpath.rsplit(".", 1)
                mod = importlib.import_module(module)
                self.assertTrue(
                    hasattr(mod, cls),
                    "{}: {} missing {}".format(category, name, drv.classpath),
                )

    def test_classpath_for(self):
        self.assertEqual(
            sc.classpath_for("laser", "TestLaser"), "monet.laser.TestLaser"
        )

    def test_nidaqmx_lines_is_required(self):
        # Blank 'lines' would KeyError in NIdaqmxAOAttenuator._connect, so the
        # field must be required so the wizard forces a value.
        drv = sc.DRIVERS["attenuation"]["NIdaqmxAOAttenuator"]
        lines = next(f for f in drv.fields if f.name == "lines")
        self.assertTrue(lines.required)

    def test_enumerate_choices_never_raises(self):
        for tag in ("serial", "visa", "nidaq", "unknown"):
            self.assertIsInstance(sc.enumerate_choices(tag), list)


class TestBuildAndPersist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_microscope_entry(self):
        entry = sc.build_microscope_entry(_sim_answers())
        self.assertEqual(entry["database"], "./wiz_power.xlsx")
        self.assertEqual(entry["index"]["name"], "wiz_sim")
        self.assertEqual(entry["index"]["wavelength [nm]"], 488)
        self.assertEqual(
            entry["powermeter"]["classpath"],
            "monet.powermeter.TestPowerMeter",
        )
        self.assertEqual(
            entry["analysis"]["classpath"],
            "monet.analysis.SinusAttenuationCurveAnalyzer",
        )
        self.assertIn(488, entry["lasers"])
        self.assertEqual(
            entry["lasers"][488]["classpath"], "monet.laser.TestLaser"
        )

    def test_build_without_optional_sections(self):
        answers = _sim_answers()
        answers.pop("lasers")
        entry = sc.build_microscope_entry(answers)
        self.assertNotIn("lasers", entry)
        self.assertNotIn("beampath", entry)

    def test_write_microscope_config_roundtrip_and_backup(self):
        import yaml

        path = os.path.join(self.tmp, "configs.yaml")
        entry = sc.build_microscope_entry(_sim_answers())
        sc.write_microscope_config("wiz_sim", entry, path)
        with open(path) as f:
            data = yaml.full_load(f)
        self.assertIn("wiz_sim", data)
        self.assertFalse(os.path.exists(path + ".bak"))

        # second write preserves other keys and backs up the prior file
        other = sc.build_microscope_entry(_sim_answers(name="wiz_two"))
        sc.write_microscope_config("wiz_two", other, path)
        with open(path) as f:
            data = yaml.full_load(f)
        self.assertEqual(set(data), {"wiz_sim", "wiz_two"})
        self.assertTrue(os.path.exists(path + ".bak"))

    def test_write_appends_to_existing_file(self):
        import yaml

        path = os.path.join(self.tmp, "configs.yaml")
        with open(path, "w") as f:
            yaml.dump({"Existing": {"database": "./x.xlsx"}}, f)
        entry = sc.build_microscope_entry(_sim_answers(name="NewRig"))
        sc.write_microscope_config("NewRig", entry, path)
        with open(path) as f:
            data = yaml.full_load(f)
        # the pre-existing entry is preserved, the new one appended
        self.assertEqual(set(data), {"Existing", "NewRig"})
        self.assertEqual(data["Existing"]["database"], "./x.xlsx")

    def test_existing_entry_names(self):
        import yaml

        self.assertEqual(sc.existing_entry_names("/no/such/file.yaml"), [])
        path = os.path.join(self.tmp, "p.yaml")
        with open(path, "w") as f:
            yaml.dump({"B": 1, "A": 2}, f)
        self.assertEqual(sc.existing_entry_names(path), ["A", "B"])

    def test_write_protocol_entry(self):
        import yaml

        path = os.path.join(self.tmp, "protocols.yaml")
        proto = {"laser_sequence": [488], "laser_powers": {488: [100, 200]}}
        sc.write_protocol_entry("wiz_sim", proto, path)
        with open(path) as f:
            data = yaml.full_load(f)
        self.assertEqual(data["wiz_sim"]["laser_sequence"], [488])

    def test_scaffold_central_folder(self):
        folder = os.path.join(self.tmp, "central")
        out = sc.scaffold_central_folder(folder)
        self.assertTrue(os.path.isfile(out["configs"]))
        self.assertTrue(os.path.isfile(out["protocols"]))
        # does not clobber an existing file
        with open(out["configs"], "w") as f:
            f.write("sentinel: 1\n")
        sc.scaffold_central_folder(folder)
        with open(out["configs"]) as f:
            self.assertEqual(f.read(), "sentinel: 1\n")

    def test_scaffold_missing_templates_raises(self):
        # Simulate a packaged install without docs/examples: scaffold must fail
        # loudly rather than return paths to files it never created.
        folder = os.path.join(self.tmp, "central2")
        missing = os.path.join(self.tmp, "no_such_examples")
        with mock.patch.object(sc, "examples_dir", return_value=missing):
            with self.assertRaises(FileNotFoundError):
                sc.scaffold_central_folder(folder)


class TestMMConfigParsing(unittest.TestCase):
    def _cfg_path(self):
        return os.path.join(sc.examples_dir(), "microscope.cfg")

    def test_parses_groups_presets_and_shutter(self):
        cfg = sc.parse_mm_config(self._cfg_path())
        # The example cfg's filter group is literally "Filter".
        self.assertIn("Filter", cfg.groups)
        presets = cfg.groups["Filter"]
        self.assertIn("2-TIRF 560", presets)
        self.assertIn("1-TIRF 488", presets)
        # presets are de-duplicated (defined across many ConfigGroup lines)
        self.assertEqual(len(presets), len(set(presets)))
        self.assertIn("Nosepiece", cfg.groups)
        self.assertEqual(cfg.core_shutter, "Turret1Shutter")

    def test_parse_from_written_cfg(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "m.cfg")
        with open(path, "w") as f:
            f.write(
                "# a comment\n"
                "ConfigGroup,Filter turret,Ti488setting,FW,Label,1\n"
                "ConfigGroup,Filter turret,Ti561setting,FW,Label,2\n"
                "ConfigGroup,Filter turret,Ti488setting,Shutter,State,1\n"
                "Property,Core,Shutter,MyShutter\n"
                "Property,Laser,Port,COM9\n"
            )
        cfg = sc.parse_mm_config(path)
        self.assertEqual(
            cfg.groups["Filter turret"], ["Ti488setting", "Ti561setting"]
        )
        self.assertEqual(cfg.core_shutter, "MyShutter")
        self.assertIn("COM9", cfg.serial_ports)

    def test_empty_group_declaration(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "m.cfg")
        with open(path, "w") as f:
            f.write("ConfigGroup,EmptyGroup\n")
        cfg = sc.parse_mm_config(path)
        self.assertEqual(cfg.groups["EmptyGroup"], [])


class TestBuildProtocol(unittest.TestCase):
    def test_build_protocol_entry(self):
        proto = sc.build_protocol_entry(
            {
                "laser_sequence": [488, 561],
                "laser_powers": {488: [50, 100], 561: [100, 200]},
                "beampath": {
                    488: {"DC": "Ti488setting", "shutter": True},
                    561: {"DC": "Ti561setting", "shutter": True},
                    "end": {"DC": "Ti488setting", "shutter": False},
                },
            }
        )
        self.assertEqual(proto["laser_sequence"], [488, 561])
        self.assertEqual(proto["laser_powers"][488], [50, 100])
        self.assertEqual(proto["beampath"][488]["DC"], "Ti488setting")
        self.assertFalse(proto["beampath"]["end"]["shutter"])

    def test_build_protocol_without_beampath(self):
        proto = sc.build_protocol_entry(
            {"laser_sequence": [488], "laser_powers": {488: [100]}}
        )
        self.assertNotIn("beampath", proto)


class TestConnectionCheck(unittest.TestCase):
    def test_test_microscope_sim_all_ok(self):
        entry = sc.build_microscope_entry(_sim_answers())
        results = sc.test_microscope(entry)
        by_device = {r.device: r for r in results}
        self.assertIn("powermeter", by_device)
        self.assertIn("attenuation", by_device)
        self.assertIn("laser 488", by_device)
        for r in results:
            self.assertTrue(r.ok, "{}: {}".format(r.device, r.detail))

    def test_test_microscope_bad_classpath(self):
        entry = sc.build_microscope_entry(_sim_answers())
        entry["powermeter"]["classpath"] = "monet.powermeter.DoesNotExist"
        results = sc.test_microscope(entry)
        pm = next(r for r in results if r.device == "powermeter")
        self.assertFalse(pm.ok)
        self.assertIn("could not connect", pm.detail)


class TestServerHelpers(unittest.TestCase):
    def test_parse_token_line(self):
        out = (
            "Created a write token for 'rig'.\n"
            "Stored in /etc/monet/monet.env ...\n\n"
            "    PAINT_MONET_TOKEN=abc123-XYZ_tok\n\n"
            "(The value was printed ...)\n"
        )
        self.assertEqual(sc.parse_token_line(out), "abc123-XYZ_tok")

    def test_parse_token_line_absent(self):
        self.assertIsNone(sc.parse_token_line("no token here\n"))

    def test_guided_instructions_mentions_params(self):
        text = sc.guided_server_instructions(
            host="srv.lab", port=9001, rig="scope-a"
        )
        self.assertIn("srv.lab", text)
        self.assertIn("9001", text)
        self.assertIn("scope-a", text)
        self.assertIn("setup-server.sh", text)

    def test_remote_install_without_paramiko_raises(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "paramiko":
                raise ModuleNotFoundError("no paramiko")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError):
                sc.remote_install_server("h", "u")


class TestFrozenPaths(unittest.TestCase):
    """The standalone (PyInstaller) build must persist .env outside the
    ephemeral _MEIPASS temp dir."""

    def setUp(self):
        self._orig = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig)

    def test_user_config_dir_honours_monet_home(self):
        os.environ["MONET_HOME"] = "/data/monet-home"
        self.assertEqual(sc.user_config_dir(), "/data/monet-home")

    def test_default_env_path_normal_install(self):
        # Not frozen: beside the package (unchanged behaviour).
        with mock.patch.object(sc.sys, "frozen", False, create=True):
            self.assertTrue(sc.default_env_path().endswith(".env"))
            self.assertIn(sc._PKG_ROOT, sc.default_env_path())

    def test_default_env_path_frozen_uses_user_dir(self):
        os.environ["MONET_HOME"] = "/data/monet-home"
        with mock.patch.object(sc.sys, "frozen", True, create=True):
            self.assertEqual(
                sc.default_env_path(), os.path.join("/data/monet-home", ".env")
            )


class TestDetectState(unittest.TestCase):
    def test_detect_state_no_network(self):
        state = sc.detect_state(probe_server=False)
        self.assertIsInstance(state, sc.SetupState)
        self.assertIsNone(state.server_status)

    def test_state_properties(self):
        unconfigured = sc.SetupState(
            env_path="/x/.env",
            env_exists=False,
            configs_path="",
            configs_loaded=False,
            server_url=None,
            server_status=None,
        )
        self.assertTrue(unconfigured.first_run)
        self.assertFalse(unconfigured.configured)

        configured = sc.SetupState(
            env_path="/x/.env",
            env_exists=True,
            configs_path="/x/configs.yaml",
            configs_loaded=True,
            server_url="http://s:8000",
            server_status={"ok": True},
        )
        self.assertFalse(configured.first_run)
        self.assertTrue(configured.configured)


if __name__ == "__main__":
    unittest.main()
