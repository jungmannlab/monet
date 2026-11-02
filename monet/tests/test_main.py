"""
monet/tests/test_main.py
~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for the CLI entry point (monet.__main__): the small utility
functions, the argparse dispatch in main(), and the configuration-editing
command handlers of MonetCalibrateInteractive.

:authors: Heinrich Grabmayr, 2024
:copyright: Copyright (c) 2024 Jungmann Lab, MPI of Biochemistry
"""

import copy
import io as _io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import pandas as pd

import monet.__main__ as mm
from monet import DATABASE_INDEXLEVELS


class TestGetMostSimilar(unittest.TestCase):

    def test_exact_match_wins(self):
        self.assertEqual(mm.get_most_similar("min", ["min", "minimum"]), "min")

    def test_substring_match(self):
        # No exact match, but 'min' is a substring of 'minimum'.
        self.assertEqual(
            mm.get_most_similar("min", ["maximum", "minimum"]), "minimum"
        )

    def test_no_match_returns_none(self):
        self.assertIsNone(mm.get_most_similar("zzz", ["min", "max"]))


class TestHelpPrinters(unittest.TestCase):

    def test_print_help_interactive(self):
        buf = _io.StringIO()
        with redirect_stdout(buf):
            mm.print_help_interactive(["database", "min", "max"])
        out = buf.getvalue()
        self.assertIn("Interactive monet.", out)
        self.assertIn("calibrate", out)
        self.assertIn("database", out)

    def test_print_help_interactive_config(self):
        buf = _io.StringIO()
        with redirect_stdout(buf):
            mm.print_help_interactive_config(["foo", "bar"])
        self.assertIn("--[CMD]", buf.getvalue())


class TestConfigLogger(unittest.TestCase):

    def test_config_logger_runs(self):
        # Writes a rotating monet.log; run it in a temp cwd so the repo stays
        # clean, then restore.
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                mm.config_logger()
                self.assertTrue(os.path.exists("monet.log"))
            finally:
                os.chdir(cwd)


def _write_excel_db(path):
    index = pd.MultiIndex.from_tuples(
        [("TestScope", 488.0, 100.0, "2024-01-01", "12:00:00")],
        names=DATABASE_INDEXLEVELS,
    )
    df = pd.DataFrame({"bkg": [0.1], "amp": [40.0]}, index=index)
    df.to_excel(path)


class TestMainDispatch(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_migrate_mode(self):
        excel = os.path.join(self.tmpdir, "src.xlsx")
        db = os.path.join(self.tmpdir, "out.db")
        _write_excel_db(excel)
        argv = ["monet", "migrate", "--source", excel, "--db-path", db]
        with mock.patch.object(sys, "argv", argv):
            mm.main()
        # The SQLite DB should now exist with the migrated record.
        self.assertTrue(os.path.exists(db))
        from sqlalchemy.orm import Session

        from monet.models import Calibration, get_engine

        with Session(get_engine(db)) as session:
            self.assertEqual(session.query(Calibration).count(), 1)

    def test_migrate_db_path_and_url_are_mutually_exclusive(self):
        argv = [
            "monet",
            "migrate",
            "--source",
            "x.xlsx",
            "--db-path",
            "a.db",
            "--url",
            "http://host:8000",
        ]
        with mock.patch.object(sys, "argv", argv):
            # argparse rejects both targets at once (SystemExit), rather than
            # silently uploading to --url.
            with self.assertRaises(SystemExit):
                mm.main()

    def test_migrate_mode_requires_source(self):
        argv = [
            "monet",
            "migrate",
            "--db-path",
            os.path.join(self.tmpdir, "x.db"),
        ]
        with mock.patch.object(sys, "argv", argv):
            # parser.error() raises SystemExit.
            with self.assertRaises(SystemExit):
                mm.main()

    def test_invalid_mode_raises_keyerror(self):
        with mock.patch.object(sys, "argv", ["monet", "bogusmode"]):
            with self.assertRaises(KeyError):
                mm.main()


class TestCalibrateInteractive(unittest.TestCase):
    """Exercises the configuration-editing handlers. Construction uses the
    built-in 'test' config (all Test* hardware). We deep-copy the live config
    onto the instance so the shared module-level CONFIGS dict is not mutated.
    """

    def _make(self):
        with redirect_stdout(_io.StringIO()):
            cli = mm.MonetCalibrateInteractive("test")
        # Isolate from the global CONFIGS['test'] dict.
        cli.pc.instrument.config = copy.deepcopy(cli.pc.instrument.config)
        return cli

    def test_unknown_config_name_raises(self):
        with redirect_stdout(_io.StringIO()):
            with self.assertRaises(KeyError):
                mm.MonetCalibrateInteractive("does-not-exist")

    def test_do_exit_returns_true(self):
        cli = self._make()
        self.assertTrue(cli.do_exit(""))

    def test_do_rename(self):
        cli = self._make()
        cli.do_rename("  Renamed  ")
        self.assertEqual(cli.config_name, "Renamed")
        self.assertEqual(cli.pc.instrument.config["index"]["name"], "Renamed")

    def test_do_config_sets_index_string(self):
        cli = self._make()
        with redirect_stdout(_io.StringIO()):
            cli.do_config("--name: Scope2")
        self.assertEqual(cli.pc.instrument.config["index"]["name"], "Scope2")

    def test_do_config_sets_numeric_analysis_param(self):
        cli = self._make()
        with redirect_stdout(_io.StringIO()):
            cli.do_config("--min: 45")
        self.assertEqual(
            cli.pc.instrument.config["analysis"]["init_kwargs"]["min"], 45.0
        )

    def test_do_config_sets_database(self):
        cli = self._make()
        with redirect_stdout(_io.StringIO()):
            cli.do_config("--database: /tmp/somewhere.xlsx")
        self.assertEqual(
            cli.pc.instrument.config["database"], "/tmp/somewhere.xlsx"
        )


if __name__ == "__main__":
    unittest.main()
