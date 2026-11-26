"""
monet/tests/test_hwstate.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tests for persistence of per-laser hardware settings.

:authors: Heinrich Grabmayr, 2026
:copyright: Copyright (c) 2026 Jungmann Lab, MPI of Biochemistry
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monet.hwstate as hwstate


class TestHwState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / "hardware_state.json"
        self._patch = mock.patch.object(hwstate, "STATE_FILE", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_load_missing_returns_empty(self):
        self.assertEqual(hwstate.load_state(), {})
        self.assertIsNone(hwstate.get_laser_state("Scope", 488))

    def test_save_and_get_roundtrip(self):
        hwstate.save_laser_state(
            "Scope", 488, laser_power=100.0, attenuator=12.3
        )
        self.assertTrue(self._path.exists())
        entry = hwstate.get_laser_state("Scope", 488)
        self.assertEqual(entry, {"laser_power": 100.0, "attenuator": 12.3})

    def test_int_and_str_laser_keys_match(self):
        """Saving with an int laser is readable with a str key and vice versa."""
        hwstate.save_laser_state("Scope", 488, laser_power=50.0)
        self.assertIsNotNone(hwstate.get_laser_state("Scope", "488"))
        self.assertIsNotNone(hwstate.get_laser_state("Scope", 488))

    def test_partial_update_preserves_other_field(self):
        hwstate.save_laser_state(
            "Scope", 488, laser_power=100.0, attenuator=12.3
        )
        # Update only the attenuator; laser_power must survive.
        hwstate.save_laser_state("Scope", 488, attenuator=45.6)
        entry = hwstate.get_laser_state("Scope", 488)
        self.assertEqual(entry["laser_power"], 100.0)
        self.assertEqual(entry["attenuator"], 45.6)

    def test_multiple_microscopes_and_lasers_isolated(self):
        hwstate.save_laser_state("A", 488, laser_power=1.0)
        hwstate.save_laser_state("A", 561, laser_power=2.0)
        hwstate.save_laser_state("B", 488, laser_power=3.0)
        self.assertEqual(hwstate.get_laser_state("A", 488)["laser_power"], 1.0)
        self.assertEqual(hwstate.get_laser_state("A", 561)["laser_power"], 2.0)
        self.assertEqual(hwstate.get_laser_state("B", 488)["laser_power"], 3.0)

    def test_save_noop_when_all_none(self):
        hwstate.save_laser_state("Scope", 488)
        self.assertFalse(self._path.exists())

    def test_corrupt_file_is_ignored(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("{not valid json")
        self.assertEqual(hwstate.load_state(), {})
        # A subsequent save still works (overwrites the corrupt file).
        hwstate.save_laser_state("Scope", 488, laser_power=10.0)
        self.assertEqual(
            hwstate.get_laser_state("Scope", 488)["laser_power"], 10.0
        )

    def test_written_file_is_valid_json(self):
        hwstate.save_laser_state(
            "Scope", 640, laser_power=200.0, attenuator=7.0
        )
        with open(self._path) as f:
            data = json.load(f)
        self.assertEqual(data["Scope"]["640"]["laser_power"], 200.0)

    def test_feedback_gains_roundtrip(self):
        hwstate.save_feedback_gains("Scope", kp=0.3, ki=0.12, max_iter=25)
        self.assertEqual(
            hwstate.get_feedback_gains("Scope"),
            {"kp": 0.3, "ki": 0.12, "max_iter": 25},
        )

    def test_feedback_gains_missing_is_none(self):
        self.assertIsNone(hwstate.get_feedback_gains("Scope"))

    def test_feedback_gains_noop_when_all_none(self):
        hwstate.save_feedback_gains("Scope")
        self.assertFalse(self._path.exists())

    def test_feedback_gains_partial_update_preserves_other(self):
        hwstate.save_feedback_gains("Scope", kp=0.3, ki=0.12, max_iter=25)
        hwstate.save_feedback_gains("Scope", ki=0.4)
        entry = hwstate.get_feedback_gains("Scope")
        self.assertEqual(entry["kp"], 0.3)
        self.assertEqual(entry["ki"], 0.4)
        self.assertEqual(entry["max_iter"], 25)

    def test_feedback_gains_do_not_collide_with_laser_state(self):
        """The gains live in a separate section and never shadow a laser."""
        hwstate.save_laser_state("Scope", 488, laser_power=100.0)
        hwstate.save_feedback_gains("Scope", kp=0.5)
        # Both coexist and read back independently.
        self.assertEqual(
            hwstate.get_laser_state("Scope", 488)["laser_power"], 100.0
        )
        self.assertEqual(hwstate.get_feedback_gains("Scope")["kp"], 0.5)


if __name__ == "__main__":
    unittest.main()
