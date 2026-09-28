"""
monet/tests/test_tokens.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The `monet token` CLI: generate/list/revoke/rotate the PAINT_MONET_TOKENS map in
a .env, in the plaintext C18 model.
"""

import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest

from monet.tokens import _read_map, token_cli


class TestTokenCLI(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = os.path.join(self.d, ".env")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.addCleanup(os.environ.pop, "PAINT_MONET_TOKENS", None)

    def _run(self, *args):
        return token_cli([*args, "--env-file", self.env])

    def test_add_creates_scoped_labelled_token(self):
        rc = self._run("add", "--scope", "write", "--label", "mercury")
        self.assertEqual(rc, 0)
        tokens = _read_map(self.env)
        self.assertEqual(len(tokens), 1)
        ((value, info),) = tokens.items()
        self.assertEqual(info.scope, "write")
        self.assertEqual(info.label, "mercury")
        # generated value avoids the map separators
        self.assertNotRegex(value, r"[:,;\s]")

    def test_env_file_is_chmod_600(self):
        self._run("add", "--scope", "read", "--label", "dash")
        mode = stat.S_IMODE(os.stat(self.env).st_mode)
        self.assertEqual(mode, 0o600)

    def test_duplicate_label_rejected(self):
        self._run("add", "--scope", "write", "--label", "m")
        rc = self._run("add", "--scope", "read", "--label", "m")
        self.assertNotEqual(rc, 0)
        # still exactly one token
        self.assertEqual(len(_read_map(self.env)), 1)

    def test_list_never_prints_token_values(self):
        self._run("add", "--scope", "read", "--label", "dash")
        ((value, _),) = _read_map(self.env).items()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._run("list")
        out = buf.getvalue()
        self.assertIn("dash", out)
        self.assertIn("read", out)
        self.assertNotIn(value, out)

    def test_revoke_removes_label(self):
        self._run("add", "--scope", "write", "--label", "m")
        rc = self._run("revoke", "--label", "m")
        self.assertEqual(rc, 0)
        self.assertEqual(_read_map(self.env), {})

    def test_revoke_unknown_label_errors(self):
        self.assertNotEqual(self._run("revoke", "--label", "nope"), 0)

    def test_rotate_changes_value_keeps_scope_label(self):
        self._run("add", "--scope", "write", "--label", "m")
        ((old_value, _),) = _read_map(self.env).items()
        rc = self._run("rotate", "--label", "m")
        self.assertEqual(rc, 0)
        ((new_value, info),) = _read_map(self.env).items()
        self.assertNotEqual(old_value, new_value)
        self.assertEqual(info.scope, "write")
        self.assertEqual(info.label, "m")

    def test_written_map_round_trips_through_shared_parser(self):
        """What the CLI writes is exactly what the server parses."""
        self._run("add", "--scope", "write", "--label", "mercury")
        self._run("add", "--scope", "read", "--label", "dash")
        from dotenv import dotenv_values

        from monet.serviceauth import AuthConfig, parse_tokens

        raw = dotenv_values(self.env)["PAINT_MONET_TOKENS"]
        parsed = parse_tokens(raw)
        self.assertEqual(len(parsed), 2)
        # a server built from this map would enforce auth with both tokens
        cfg = AuthConfig(parsed)
        self.assertTrue(cfg.enabled)
        scopes = {i.scope for i in parsed.values()}
        self.assertEqual(scopes, {"read", "write"})


if __name__ == "__main__":
    unittest.main()
