"""
monet/tests/test_beampath.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test the beam path module of monet.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import unittest

import monet.beampath as mbp


class TestBeampath(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_01_TestShutter(self):

        shutterpars = {"port": "COM4"}

        sh = mbp.TestShutter(shutterpars)

        print(sh.position)
        sh.position = True
        print(sh.position)

    def test_02_TestBeamPath(self):
        bp_config = {
            "shutter01": {
                "classpath": "monet.beampath.TestShutter",
                "init_kwargs": {"SN": 1234},
            },
        }
        bp_settings = {"A": {"shutter01": True}, "B": {"shutter01": False}}

        bp = mbp.BeamPath(bp_config)

        bp.positions = bp_settings["A"]
        bp.positions = bp_settings["B"]

    def test_03_TestShutter_autoshutter(self):
        sh = mbp.TestShutter({"SN": 1234})
        # Defaults to on; round-trips through the property.
        self.assertTrue(sh.autoshutter)
        sh.autoshutter = False
        self.assertFalse(sh.autoshutter)

    def test_03b_TestShutter_position_toggles_autoshutter(self):
        sh = mbp.TestShutter({"SN": 1234})
        # Opening the shutter switches autoshutter off so monet can hold it
        # open; closing it hands control back to the microscope.
        sh.position = True
        self.assertFalse(sh.autoshutter)
        sh.position = False
        self.assertTrue(sh.autoshutter)

    def test_04_TestShutter_position_must_be_bool(self):
        sh = mbp.TestShutter({"SN": 1234})
        with self.assertRaises(ValueError):
            sh.position = 5  # not a bool

    def test_05_BeamPath_positions_getter(self):
        bp = mbp.BeamPath(
            {
                "shutter01": {
                    "classpath": "monet.beampath.TestShutter",
                    "init_kwargs": {"SN": 1234},
                },
            }
        )
        bp.positions = {"shutter01": True}
        # The getter reflects what was set on each object.
        self.assertEqual(bp.positions, {"shutter01": True})
