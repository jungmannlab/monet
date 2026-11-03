"""
monet/tests/test_updates.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for :mod:`monet.updates` — the GitHub release update check. The
network call is monkeypatched, so these run offline.
"""

import unittest
from unittest import mock

from monet import updates


class TestVersionHelpers(unittest.TestCase):
    def test_normalize_strips_leading_v(self):
        self.assertEqual(updates._normalize("v0.6.1"), "0.6.1")
        self.assertEqual(updates._normalize("0.6.1"), "0.6.1")
        self.assertEqual(updates._normalize("  V1.2.3 "), "1.2.3")

    def test_is_newer(self):
        self.assertTrue(updates._is_newer("0.7.0", "0.6.1"))
        self.assertTrue(updates._is_newer("0.6.2", "0.6.1"))
        self.assertFalse(updates._is_newer("0.6.1", "0.6.1"))
        self.assertFalse(updates._is_newer("0.6.0", "0.6.1"))

    def test_is_newer_handles_unknown_and_empty(self):
        # An untagged source tree reports "unknown"; never flag an update.
        self.assertFalse(updates._is_newer("0.7.0", "unknown"))
        self.assertFalse(updates._is_newer("", "0.6.1"))
        self.assertFalse(updates._is_newer("0.7.0", ""))


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestCheckForUpdate(unittest.TestCase):
    def test_update_available(self):
        resp = _FakeResp({"tag_name": "v0.7.0"})
        with mock.patch("requests.get", return_value=resp):
            result = updates.check_for_update(current="0.6.1")
        self.assertEqual(result["latest"], "0.7.0")
        self.assertTrue(result["update_available"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["url"], updates.RELEASES_URL)

    def test_up_to_date(self):
        resp = _FakeResp({"tag_name": "v0.6.1"})
        with mock.patch("requests.get", return_value=resp):
            result = updates.check_for_update(current="0.6.1")
        self.assertEqual(result["latest"], "0.6.1")
        self.assertFalse(result["update_available"])
        self.assertIsNone(result["error"])

    def test_network_error_is_reported_not_raised(self):
        with mock.patch("requests.get", side_effect=OSError("offline")):
            result = updates.check_for_update(current="0.6.1")
        self.assertIsNone(result["latest"])
        self.assertFalse(result["update_available"])
        self.assertIn("offline", result["error"])

    def test_defaults_to_installed_version(self):
        resp = _FakeResp({"tag_name": "v0.0.0"})
        with mock.patch("requests.get", return_value=resp):
            result = updates.check_for_update()
        import monet

        self.assertEqual(result["current"], monet.__version__)


if __name__ == "__main__":
    unittest.main()
