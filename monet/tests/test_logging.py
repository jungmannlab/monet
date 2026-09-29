"""
monet/tests/test_logging.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

config_logger must honour MONET_LOG_FILE / MONET_LOG_DIR and must never crash
`import monet` on an unwritable log path (e.g. a systemd service with CWD=/
under ProtectSystem=strict).
"""

import logging
import os
import tempfile
import unittest
from unittest import mock

import monet


class TestConfigLogger(unittest.TestCase):
    def setUp(self):
        # config_logger() appends handlers to the shared "monet" logger;
        # snapshot and restore so tests don't leak handlers into each other.
        self._logger = logging.getLogger("monet")
        self._saved = list(self._logger.handlers)

    def tearDown(self):
        self._logger.handlers = self._saved

    def test_log_dir_env_places_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"MONET_LOG_DIR": d}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("MONET_LOG_FILE", None)
                monet.config_logger()
            self.assertTrue(os.path.exists(os.path.join(d, "monet.log")))

    def test_log_file_env_takes_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "custom.log")
            env = {"MONET_LOG_FILE": path, "MONET_LOG_DIR": "/nope"}
            with mock.patch.dict(os.environ, env, clear=False):
                monet.config_logger()
            self.assertTrue(os.path.exists(path))

    def test_unwritable_path_does_not_raise(self):
        bad = "/this/dir/does/not/exist/monet.log"
        env = {"MONET_LOG_FILE": bad}
        with mock.patch.dict(os.environ, env, clear=False):
            monet.config_logger()  # must not raise
        self.assertFalse(os.path.exists(bad))
        # a plain stream fallback handler was added (FileHandler subclasses
        # StreamHandler, so exclude it)
        handlers = logging.getLogger("monet").handlers
        self.assertTrue(
            any(
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                for h in handlers
            )
        )


if __name__ == "__main__":
    unittest.main()
