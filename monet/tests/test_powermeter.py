"""
monet/tests/test_powermeter.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test the powermeter module of monet.

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import unittest
from unittest import mock

import numpy as np

import monet.powermeter as mpm


class _FakeTLPM:
    """Minimal stand-in for Thorlabs' ``TLPM`` wrapper class.

    Simulates a single connected meter so ``ThorlabsTLPMPowerMeter`` can be
    exercised without the hardware, the TLPM driver, or Thorlabs' TLPM.py.
    Each method writes through its byref arguments like the real wrapper; the
    byref target is reachable via ``._obj`` in CPython.
    """

    _power_w = 0.0123  # 12.3 mW
    _device_count = 1

    def __init__(self):
        self._wavelength = 488.0
        self.power_autorange = None  # records setPowerAutoRange() calls

    def setPowerAutoRange(self, mode):
        self.power_autorange = mode.value

    def findRsrc(self, count_ref):
        count_ref._obj.value = self._device_count

    def getRsrcName(self, index, resource_buffer):
        resource_buffer.value = b"FAKE::TLPM::INSTR"

    def open(self, resource, id_query, reset):
        pass

    def measPower(self, power_ref):
        power_ref._obj.value = self._power_w

    def getWavelength(self, attribute, wl_ref):
        wl_ref._obj.value = self._wavelength

    def setWavelength(self, value):
        self._wavelength = value.value

    def close(self):
        pass


class _FakeTLPMNoDevice(_FakeTLPM):
    _device_count = 0


class _FakeTLPMOverRange(_FakeTLPM):
    # SCPI / IEEE-488.2 over-range sentinel (9.9e37 W) for a saturated channel.
    _power_w = 9.9e37


def _patch_wrapper(wrapper_cls):
    """Patch _import_tlpm_wrapper to return the given fake wrapper class."""
    return mock.patch.object(
        mpm.ThorlabsTLPMPowerMeter,
        "_import_tlpm_wrapper",
        return_value=wrapper_cls,
    )


class TestPowerMeter(unittest.TestCase):

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def test_basics_01_TestPowerMeter(self):
        config = {
            "bkg": 0,
            "amp": 50,
            "phi": 30,
            "start": 10,
            "step": 5,
            "noise": 3,
        }
        att = mpm.TestPowerMeter(config)

        for i in range(20):
            print(att.read())

        assert True

    def test_basics_02_ThorlabsTLPMPowerMeter(self):
        with _patch_wrapper(_FakeTLPM):
            pm = mpm.ThorlabsTLPMPowerMeter({"address": "find connection"})

            # measPower returns watts; the class reports mW.
            self.assertAlmostEqual(pm.read(averaging=5), 12.3, places=6)
            self.assertEqual(pm.unit, "mW")

            self.assertAlmostEqual(pm.wavelength, 488.0)
            pm.wavelength = 561
            self.assertAlmostEqual(pm.wavelength, 561.0)

    def test_basics_02b_ThorlabsTLPM_overrange_is_nan(self):
        # A saturated channel returns the 9.9e37 SCPI over-range sentinel;
        # read() must normalize that to NaN (not ~1e41 mW) so the caller can
        # reject the point instead of corrupting the calibration fit.
        with _patch_wrapper(_FakeTLPMOverRange):
            pm = mpm.ThorlabsTLPMPowerMeter({"address": "find connection"})
            self.assertTrue(np.isnan(pm.read(averaging=5)))

    def test_basics_02c_ThorlabsTLPM_autorange_default_on(self):
        # By default the meter is put into power auto-range at open, so a
        # too-low fixed range can't silently saturate during calibration.
        with _patch_wrapper(_FakeTLPM):
            pm = mpm.ThorlabsTLPMPowerMeter({"address": "find connection"})
            self.assertEqual(pm.pm.power_autorange, 1)

    def test_basics_02d_ThorlabsTLPM_autorange_opt_out(self):
        # power_autorange=False leaves the device range untouched.
        with _patch_wrapper(_FakeTLPM):
            pm = mpm.ThorlabsTLPMPowerMeter(
                {"address": "find connection", "power_autorange": False}
            )
            self.assertIsNone(pm.pm.power_autorange)

    def test_basics_03_ThorlabsTLPM_no_device_raises(self):
        with _patch_wrapper(_FakeTLPMNoDevice):
            with self.assertRaises(ValueError):
                mpm.ThorlabsTLPMPowerMeter({"address": "find connection"})

    def test_basics_04_ThorlabsTLPM_dll_path(self):
        # A dll_path directory should be accepted and added to the DLL search
        # path without breaking connection (cross-platform: add_dll_directory
        # is Windows-only and skipped elsewhere).
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as dll_dir:
            with _patch_wrapper(_FakeTLPM):
                pm = mpm.ThorlabsTLPMPowerMeter(
                    {"address": "find connection", "dll_path": dll_dir}
                )
                self.assertAlmostEqual(pm.read(averaging=1), 12.3, places=6)
            self.assertIn(dll_dir, os.environ.get("PATH", ""))
