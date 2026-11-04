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
import sys
import tempfile
import types
import unittest
from unittest import mock

from monet import setup_core as sc


def _fake_paramiko(record, outputs, exit_status=0):
    """A minimal fake `paramiko` module for remote_install_server tests.

    Records opened channels (cmd + what was sent on stdin) in ``record`` and
    replies with the first ``outputs`` value whose key is a substring of cmd.
    """

    class _Chan:
        def __init__(self):
            self.cmd = None
            self.sent = b""
            self._buf = b""
            self._read = False

        def set_combine_stderr(self, v):
            pass

        def exec_command(self, cmd):
            self.cmd = cmd
            out = ""
            for key, val in outputs.items():
                if key in cmd:
                    out = val
                    break
            self._buf = out.encode()

        def sendall(self, data):
            self.sent += data

        def shutdown_write(self):
            pass

        def recv(self, n):
            if self._read:
                return b""
            self._read = True
            return self._buf

        def recv_exit_status(self):
            return exit_status

        def close(self):
            pass

    class _Transport:
        def open_session(self):
            ch = _Chan()
            record["channels"].append(ch)
            return ch

    class _Client:
        def set_missing_host_key_policy(self, p):
            pass

        def connect(self, **kw):
            record["connect_kwargs"] = kw

        def get_transport(self):
            return _Transport()

        def close(self):
            pass

    return types.SimpleNamespace(
        SSHClient=lambda: _Client(), AutoAddPolicy=lambda: object()
    )


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


class TestBeampathHelpers(unittest.TestCase):
    def test_groups_with_presets(self):
        self.assertEqual(sc.groups_with_presets(None), [])
        cfg = sc.MMConfig(
            path="x",
            groups={"Filter": ["a", "b"], "Empty": []},
            core_shutter=None,
            serial_ports=[],
        )
        self.assertEqual(sc.groups_with_presets(cfg), ["Filter"])

    def test_is_shutter_beampath(self):
        self.assertTrue(sc.is_shutter_beampath({"class": "NikonShutter"}))
        self.assertTrue(sc.is_shutter_beampath({"class": "TestShutter"}))
        self.assertFalse(sc.is_shutter_beampath({"class": "NikonFilterWheel"}))

    def test_assemble_protocol_beampath(self):
        bp_objs = {
            "DC": {"class": "NikonFilterWheel"},
            "shutter": {"class": "NikonShutter"},
        }
        presets = {
            (488, "DC"): "Ti488",
            (561, "DC"): "Ti561",
            ("end", "DC"): "Ti488",
        }
        bp = sc.assemble_protocol_beampath(
            bp_objs,
            [488, 561],
            preset_for=lambda step, obj: presets[(step, obj)],
        )
        self.assertEqual(bp[488], {"DC": "Ti488", "shutter": True})
        self.assertEqual(bp[561], {"DC": "Ti561", "shutter": True})
        # default shutter rule: closed at the end step
        self.assertEqual(bp["end"], {"DC": "Ti488", "shutter": False})


class TestUpgradeHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_server_database(self):
        self.assertTrue(sc.is_server_database("http://h:8000"))
        self.assertTrue(sc.is_server_database("https://h"))
        self.assertFalse(sc.is_server_database("./x.xlsx"))
        self.assertFalse(sc.is_server_database(None))

    def test_local_db_microscopes(self):
        configs = {
            "A": {"database": "./a.xlsx"},
            "B": {"database": "http://srv:8000"},
            "C": {"database": "/abs/c.xlsx"},
        }
        with (
            mock.patch.object(sc.monet, "CONFIGS", configs),
            mock.patch.object(sc.monet, "CONFIGS_PATH", "/cfg/configs.yaml"),
        ):
            self.assertEqual(set(sc.local_db_microscopes()), {"A", "C"})

    def test_local_db_microscopes_empty_on_builtins(self):
        with mock.patch.object(sc.monet, "CONFIGS_PATH", ""):
            self.assertEqual(sc.local_db_microscopes(), {})

    def test_resolve_local_db_path(self):
        cfgdir = os.path.join(self.tmp, "cfg")
        os.makedirs(cfgdir)
        dbfile = os.path.join(cfgdir, "power.xlsx")
        open(dbfile, "w").close()
        configs_path = os.path.join(cfgdir, "configs.yaml")
        self.assertEqual(
            sc.resolve_local_db_path("./power.xlsx", configs_path), dbfile
        )
        self.assertIsNone(sc.resolve_local_db_path("http://h", configs_path))
        self.assertIsNone(
            sc.resolve_local_db_path("./missing.xlsx", configs_path)
        )

    def test_repoint_microscope_to_server(self):
        import yaml

        path = os.path.join(self.tmp, "configs.yaml")
        configs = {
            "A": {"database": "./a.xlsx", "index": {"name": "A"}},
            "B": {"database": "./b.xlsx"},
        }
        with open(path, "w") as f:
            yaml.dump(configs, f)
        with mock.patch.object(sc.monet, "CONFIGS", configs):
            sc.repoint_microscope_to_server("A", "http://srv:8000", path)
        with open(path) as f:
            data = yaml.full_load(f)
        self.assertEqual(data["A"]["database"], "http://srv:8000")
        self.assertEqual(data["A"]["index"]["name"], "A")  # preserved
        self.assertEqual(data["B"]["database"], "./b.xlsx")  # untouched


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

    def test_server_token_help_lists_commands(self):
        text = sc.server_token_help("http://srv.lab:8000")
        for frag in (
            "token add",
            "token list",
            "token rotate",
            "token revoke",
            "--scope read",
            "--scope write",
            sc.SERVER_ENV_FILE,
            "srv.lab",
            "/dashboard",
            "systemctl restart monet",
        ):
            self.assertIn(frag, text)

    def test_repoint_reads_from_file_not_only_memory(self):
        # repoint must work on an entry the wizard just wrote to the file even
        # if it isn't in the import-time monet.CONFIGS.
        import yaml

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = os.path.join(tmp, "configs.yaml")
        with open(cfg, "w") as f:
            yaml.dump({"Rig": {"database": "./r.xlsx"}}, f)
        path = sc.repoint_microscope_to_server("Rig", "http://h:8000", cfg)
        self.assertEqual(path, cfg)
        with open(cfg) as f:
            self.assertEqual(
                yaml.full_load(f)["Rig"]["database"], "http://h:8000"
            )

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

    def test_remote_install_feeds_sudo_password_over_ssh(self):
        # Regression: sudo over an exec channel must be fed its password on
        # stdin (sudo -S), or the install fails with "sudo: Authentication
        # failed". Drive it against a fake paramiko.
        record = {"channels": [], "connect_kwargs": None}

        class _Chan:
            def __init__(self, outputs):
                self._outputs = outputs
                self.cmd = None
                self.sent = b""
                self._buf = b""
                self._read = False

            def set_combine_stderr(self, v):
                pass

            def exec_command(self, cmd):
                self.cmd = cmd
                out = ""
                for key, val in self._outputs.items():
                    if key in cmd:
                        out = val
                        break
                self._buf = out.encode()

            def sendall(self, data):
                self.sent += data

            def shutdown_write(self):
                pass

            def recv(self, n):
                if self._read:
                    return b""
                self._read = True
                return self._buf

            def recv_exit_status(self):
                return 0

            def close(self):
                pass

        outputs = {
            "token add": "PAINT_MONET_TOKEN=tok-xyz\n",
            # service came up after restart (health-check probe marker)
            "MONET_IS_ACTIVE": "MONET_IS_ACTIVE=active\n",
        }

        class _Transport:
            def open_session(self):
                ch = _Chan(outputs)
                record["channels"].append(ch)
                return ch

        class _Client:
            def set_missing_host_key_policy(self, p):
                pass

            def connect(self, **kw):
                record["connect_kwargs"] = kw

            def get_transport(self):
                return _Transport()

            def close(self):
                pass

        fake_paramiko = types.SimpleNamespace(
            SSHClient=lambda: _Client(),
            AutoAddPolicy=lambda: object(),
        )
        with mock.patch.dict(sys.modules, {"paramiko": fake_paramiko}):
            result = sc.remote_install_server(
                "192.168.0.5", "linuxmp", password="secret", rig="scope-a"
            )

        self.assertEqual(
            result, {"url": "http://192.168.0.5:8000", "token": "tok-xyz"}
        )
        # Password-only auth was forced (no agent/default-key probing).
        self.assertFalse(record["connect_kwargs"]["look_for_keys"])
        self.assertFalse(record["connect_kwargs"]["allow_agent"])
        # Every sudo command used `sudo -S` and was fed the password on stdin.
        sudo_chans = [c for c in record["channels"] if "sudo -S" in c.cmd]
        self.assertTrue(sudo_chans)
        for c in sudo_chans:
            self.assertEqual(c.sent, b"secret\n")
        # The install passes the script's config via `env` (sudo-rs doesn't
        # accept `sudo VAR=value command`), not on the sudo command line.
        install_cmds = [
            c.cmd for c in record["channels"] if "setup-server" in c.cmd
        ]
        self.assertTrue(install_cmds)
        self.assertIn("sudo -S env GIT_REF=", install_cmds[0])

    def test_remote_install_fails_if_service_not_active(self):
        # `systemctl restart` returns 0 but the unit then fails: is-active
        # reports 'failed' -> the install must NOT report success.
        record = {"channels": [], "connect_kwargs": None}
        fake = _fake_paramiko(
            record,
            {
                "token add": "PAINT_MONET_TOKEN=tok\n",
                "MONET_IS_ACTIVE": "MONET_IS_ACTIVE=failed\n",
                "status monet": "monet.service: failed\n",
            },
        )
        with mock.patch.dict(sys.modules, {"paramiko": fake}):
            with self.assertRaises(RuntimeError) as cm:
                sc.remote_install_server("h", "u", password="p")
        self.assertIn("did not come up", str(cm.exception))

    def test_remote_mint_token_over_ssh(self):
        # Onboarding a rig against an already-running server: mint a token over
        # SSH WITHOUT reinstalling anything.
        record = {"channels": [], "connect_kwargs": None}
        fake = _fake_paramiko(
            record,
            {
                "token add": "PAINT_MONET_TOKEN=minted-tok\n",
                "MONET_IS_ACTIVE": "MONET_IS_ACTIVE=active\n",
            },
        )
        with mock.patch.dict(sys.modules, {"paramiko": fake}):
            token = sc.remote_mint_token(
                "192.168.0.9", "lab", password="pw", rig="rigA"
            )
        self.assertEqual(token, "minted-tok")
        cmds = [c.cmd for c in record["channels"]]
        # It minted a token for the rig and restarted — but did NOT reinstall.
        self.assertTrue(any("token add" in c and "rigA" in c for c in cmds))
        self.assertTrue(any("systemctl restart monet" in c for c in cmds))
        self.assertFalse(any("setup-server" in c for c in cmds))
        # Every sudo command was fed the password on stdin.
        sudo_chans = [c for c in record["channels"] if "sudo -S" in c.cmd]
        self.assertTrue(sudo_chans)
        for c in sudo_chans:
            self.assertEqual(c.sent, b"pw\n")

    def test_remote_mint_token_fails_if_service_not_active(self):
        record = {"channels": [], "connect_kwargs": None}
        fake = _fake_paramiko(
            record,
            {
                "token add": "PAINT_MONET_TOKEN=tok\n",
                "MONET_IS_ACTIVE": "MONET_IS_ACTIVE=failed\n",
                "status monet": "monet.service: failed\n",
            },
        )
        with mock.patch.dict(sys.modules, {"paramiko": fake}):
            with self.assertRaises(RuntimeError) as cm:
                sc.remote_mint_token("h", "u", password="p")
        self.assertIn("did not come up", str(cm.exception))

    def test_remote_mint_token_without_paramiko_raises(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "paramiko":
                raise ModuleNotFoundError("no paramiko")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError):
                sc.remote_mint_token("h", "u")

    def test_remote_update_server_over_ssh(self):
        # In-place upgrade of a running server: re-run setup-server.sh + restart
        # + health-check, but DON'T mint a token (the rig keeps its own).
        record = {"channels": [], "connect_kwargs": None}
        fake = _fake_paramiko(
            record,
            {"MONET_IS_ACTIVE": "MONET_IS_ACTIVE=active\n"},
        )
        with mock.patch.dict(sys.modules, {"paramiko": fake}):
            result = sc.remote_update_server(
                "192.168.0.9", "lab", password="pw", port=9001
            )
        self.assertEqual(result, {"url": "http://192.168.0.9:9001"})
        self.assertNotIn("token", result)
        cmds = [c.cmd for c in record["channels"]]
        # It re-ran the installer and restarted, but minted no token.
        self.assertTrue(any("setup-server.sh" in c for c in cmds))
        self.assertTrue(any("MONET_PORT=9001" in c for c in cmds))
        self.assertTrue(any("systemctl restart monet" in c for c in cmds))
        self.assertFalse(any("token add" in c for c in cmds))
        sudo_chans = [c for c in record["channels"] if "sudo -S" in c.cmd]
        self.assertTrue(sudo_chans)
        for c in sudo_chans:
            self.assertEqual(c.sent, b"pw\n")

    def test_remote_update_server_fails_if_service_not_active(self):
        record = {"channels": [], "connect_kwargs": None}
        fake = _fake_paramiko(
            record,
            {
                "MONET_IS_ACTIVE": "MONET_IS_ACTIVE=failed\n",
                "status monet": "monet.service: failed\n",
            },
        )
        with mock.patch.dict(sys.modules, {"paramiko": fake}):
            with self.assertRaises(RuntimeError) as cm:
                sc.remote_update_server("h", "u", password="p")
        self.assertIn("did not come up", str(cm.exception))

    def test_remote_update_server_without_paramiko_raises(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "paramiko":
                raise ModuleNotFoundError("no paramiko")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError):
                sc.remote_update_server("h", "u")


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
