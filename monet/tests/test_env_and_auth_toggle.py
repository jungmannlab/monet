"""
monet/tests/test_env_and_auth_toggle.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.env loading, config/protocol path resolution from the environment (replacing
the deprecated env.yaml), and the PAINT_MONET_AUTH on/off/auto toggle.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import monet
import monet.io as mio


def _clear(*names):
    for n in names:
        os.environ.pop(n, None)


class TestEnvLoading(unittest.TestCase):
    def tearDown(self):
        _clear("MONET_TEST_A", "MONET_TEST_B", "MONET_CONFIG_PATHS")

    def test_load_env_files_populates_without_override(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("MONET_TEST_A=fromenv\nMONET_TEST_B=fromenv\n")
        os.environ["MONET_TEST_B"] = "preset"  # already set → must win
        monet._load_env_files(pkg_root=d)
        self.assertEqual(os.environ.get("MONET_TEST_A"), "fromenv")
        self.assertEqual(os.environ.get("MONET_TEST_B"), "preset")

    def test_paths_from_env(self):
        self.assertIsNone(monet._paths_from_env("MONET_CONFIG_PATHS"))
        os.environ["MONET_CONFIG_PATHS"] = os.pathsep.join(
            ["/a/configs.yaml", "  ", "/b/configs.yaml"]
        )
        self.assertEqual(
            monet._paths_from_env("MONET_CONFIG_PATHS"),
            ["/a/configs.yaml", "/b/configs.yaml"],
        )


class TestAuthMode(unittest.TestCase):
    def tearDown(self):
        _clear("PAINT_MONET_AUTH")

    def test_auth_mode_parsing(self):
        cases = {
            None: "auto",
            "": "auto",
            "weird": "auto",
            "off": "off",
            "OFF": "off",
            "0": "off",
            "false": "off",
            "no": "off",
            "on": "on",
            "1": "on",
            "true": "on",
            "yes": "on",
        }
        for value, expected in cases.items():
            _clear("PAINT_MONET_AUTH")
            if value is not None:
                os.environ["PAINT_MONET_AUTH"] = value
            self.assertEqual(monet.auth_mode(), expected, value)


class TestServiceAuthToggle(unittest.TestCase):
    def tearDown(self):
        _clear("PAINT_MONET_AUTH", "PAINT_MONET_TOKENS")

    def test_off_disables_even_with_tokens(self):
        from monet.serviceauth import auth_from_env

        os.environ["PAINT_MONET_TOKENS"] = "wtok:write:x"
        os.environ["PAINT_MONET_AUTH"] = "off"
        self.assertFalse(auth_from_env().enabled)

    def test_on_enabled_with_tokens(self):
        from monet.serviceauth import auth_from_env

        os.environ["PAINT_MONET_TOKENS"] = "wtok:write:x"
        os.environ["PAINT_MONET_AUTH"] = "on"
        self.assertTrue(auth_from_env().enabled)

    def test_on_without_tokens_is_disabled_here(self):
        # (the serve CLI guard turns this into a hard error; the helper itself
        # must not raise, so importing the app never fails)
        from monet.serviceauth import auth_from_env

        os.environ["PAINT_MONET_AUTH"] = "on"
        self.assertFalse(auth_from_env().enabled)

    def test_auto_follows_token_presence(self):
        from monet.serviceauth import auth_from_env

        os.environ["PAINT_MONET_AUTH"] = "auto"
        self.assertFalse(auth_from_env().enabled)
        os.environ["PAINT_MONET_TOKENS"] = "wtok:write:x"
        self.assertTrue(auth_from_env().enabled)


class TestIOClientToggle(unittest.TestCase):
    def tearDown(self):
        _clear("PAINT_MONET_AUTH", "PAINT_MONET_TOKEN")

    def test_off_sends_no_token(self):
        os.environ["PAINT_MONET_TOKEN"] = "wtok"
        os.environ["PAINT_MONET_AUTH"] = "off"
        self.assertEqual(mio._auth_headers(), {})

    def test_auto_sends_token_when_set(self):
        os.environ["PAINT_MONET_TOKEN"] = "wtok"
        os.environ["PAINT_MONET_AUTH"] = "auto"
        self.assertEqual(mio._auth_headers(), {"Authorization": "Bearer wtok"})

    def test_no_token_no_header(self):
        _clear("PAINT_MONET_TOKEN")
        self.assertEqual(mio._auth_headers(), {})


class TestCreateAppToggle(unittest.TestCase):
    def tearDown(self):
        _clear("PAINT_MONET_AUTH", "PAINT_MONET_TOKENS")

    def test_off_disables_app_auth_even_with_tokens(self):
        from monet.server import create_app

        os.environ["PAINT_MONET_TOKENS"] = "wtok:write:x"
        os.environ["PAINT_MONET_AUTH"] = "off"
        app = create_app()
        self.assertFalse(app.state.auth.enabled)


class TestAuthReload(unittest.TestCase):
    """SIGHUP live-reload: server.reload_auth picks up .env token changes."""

    def tearDown(self):
        _clear("PAINT_MONET_AUTH", "PAINT_MONET_TOKENS")

    def test_reload_auth_picks_up_new_tokens_from_env_file(self):
        from monet.server import create_app, reload_auth

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        _clear("PAINT_MONET_TOKENS", "PAINT_MONET_AUTH")
        os.chdir(d)

        app = create_app()
        self.assertFalse(app.state.auth.enabled)  # no tokens yet

        # an admin runs `monet token add`, writing the .env in the server's cwd
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("PAINT_MONET_TOKENS=wtok:write:mercury\n")

        cfg = reload_auth(app)  # what the SIGHUP handler calls
        self.assertTrue(cfg.enabled)
        self.assertIs(app.state.auth, cfg)
        self.assertEqual(app.state.auth.resolve("wtok").label, "mercury")

    def test_install_auth_reload_installs_on_unix(self):
        import signal

        from monet.server import create_app, install_auth_reload

        app = create_app()
        installed = install_auth_reload(app)
        if hasattr(signal, "SIGHUP"):
            self.assertTrue(installed)
            self.assertTrue(callable(signal.getsignal(signal.SIGHUP)))
            # restore default so we don't leak the handler into other tests
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
        else:  # Windows
            self.assertFalse(installed)


class TestServeGuardToggle(unittest.TestCase):
    def tearDown(self):
        _clear("PAINT_MONET_AUTH", "PAINT_MONET_TOKENS")

    def test_on_without_tokens_refuses_even_on_loopback(self):
        import monet.__main__ as mmain

        _clear("PAINT_MONET_TOKENS")
        os.environ["PAINT_MONET_AUTH"] = "on"
        with mock.patch.object(
            sys, "argv", ["monet", "serve", "--host", "127.0.0.1"]
        ):
            with self.assertRaises(SystemExit):
                mmain.main()

    def test_off_still_refuses_non_loopback(self):
        import monet.__main__ as mmain

        _clear("PAINT_MONET_TOKENS")
        os.environ["PAINT_MONET_AUTH"] = "off"
        with mock.patch.object(
            sys, "argv", ["monet", "serve", "--host", "0.0.0.0"]
        ):
            with self.assertRaises(SystemExit):
                mmain.main()

    def test_on_with_tokens_serves_non_loopback(self):
        import monet.__main__ as mmain

        os.environ["PAINT_MONET_AUTH"] = "on"
        os.environ["PAINT_MONET_TOKENS"] = "wtok:write:x"
        with mock.patch.object(
            sys, "argv", ["monet", "serve", "--host", "0.0.0.0"]
        ):
            with mock.patch("uvicorn.run") as run:
                mmain.main()
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
