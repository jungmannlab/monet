"""
monet/tests/test_setup_cli.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Drive the ``monet setup`` interactive wizard with scripted ``input()`` answers
and assert it writes the expected ``.env`` + ``configs.yaml``. The server probe
and (where exercised) the SSH install are patched out; the hardware check runs
against the real ``Test*`` simulation classes.
"""

import io as _io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

from monet import setup_cli
from monet import setup_core as sc


class TestConnectExistingAndAuthor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env_file = os.path.join(self.tmp, ".env")
        self.folder = os.path.join(self.tmp, "central")
        self._orig = dict(os.environ)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._orig)

    def test_full_connect_and_author_sim(self):
        answers = [
            "y",  # server already exists?
            "http://localhost:8000",  # server URL
            self.folder,  # central folder
            "",  # copy templates? -> default no
            "",  # author microscope? -> default yes
            "wiz_sim",  # name
            "",  # database -> default (server url)
            "",  # wavelength -> 488
            "",  # laser_power -> 100
            "1",  # powermeter -> TestPowerMeter
            "",
            "",
            "",
            "",
            "",
            "",
            "",  # 7 powermeter fields
            "1",  # attenuation -> TestAttenuator
            "",
            "",
            "",
            "",
            "",  # 5 attenuation fields
            "1",  # analysis -> Sinus
            "",
            "",
            "",  # 3 analysis fields
            "",  # configure lasers? -> no
            "",  # configure beampath? -> no
            "",  # test hardware? -> yes
        ]
        verdict = {
            "reachable": True,
            "ok": True,
            "auth_enabled": False,
            "detail": "server auth is disabled",
        }
        with (
            mock.patch("builtins.input", side_effect=answers),
            mock.patch("monet.setup_core.check_server", return_value=verdict),
            redirect_stdout(_io.StringIO()),
        ):
            rc = setup_cli.run(env_file=self.env_file)
        self.assertEqual(rc, 0)

        env = sc.read_env(self.env_file)
        configs_path = os.path.join(self.folder, "configs.yaml")
        self.assertEqual(
            env[sc.ENV_CONFIG_PATHS], sc.paths_to_env([configs_path])
        )
        self.assertIn(sc.ENV_PROTOCOL_PATHS, env)

        with open(configs_path) as f:
            configs = yaml.full_load(f)
        self.assertIn("wiz_sim", configs)
        entry = configs["wiz_sim"]
        self.assertEqual(entry["database"], "http://localhost:8000")
        self.assertEqual(
            entry["powermeter"]["classpath"],
            "monet.powermeter.TestPowerMeter",
        )
        self.assertEqual(entry["index"]["wavelength [nm]"], 488)


class TestNewServerGuidedFallback(unittest.TestCase):
    def tearDown(self):
        for k in (sc.ENV_CLIENT_TOKEN, sc.ENV_AUTH):
            os.environ.pop(k, None)

    def test_guided_fallback_collects_token(self):
        answers = [
            "n",  # server already exists? -> no (new server)
            "srv.lab",  # hostname
            "",  # port -> 8000
            "",  # rig label -> default
            "",  # install over SSH? -> default no
            "y",  # have you run the steps + have a token?
            "tok-abc",  # paste token
        ]
        updates = {}
        with (
            mock.patch("builtins.input", side_effect=answers),
            redirect_stdout(_io.StringIO()),
        ):
            url = setup_cli._step_server(updates)
        self.assertEqual(url, "http://srv.lab:8000")
        self.assertEqual(updates[sc.ENV_CLIENT_TOKEN], "tok-abc")
        self.assertEqual(updates[sc.ENV_AUTH], "on")


if __name__ == "__main__":
    unittest.main()
