#!/usr/bin/env python
"""
monet/control.py
~~~~~~~~~~~~~~~~

Here, the calibrated system is controlled, correct laser power
and attenuations are set for a set output power

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import logging

import numpy as np
import pandas as pd
from icecream import ic

import monet.hwstate as hwstate
import monet.io as io
from monet import (
    DEVICE_TAG,
    LASER_TAG,
    POWER_TAG,
    POWERMETER_BFP,
    normalize_powermeter_type,
)
from monet.beampath import BeamPath
from monet.util import load_class, release_hardware

logger = logging.getLogger(__name__)
ic.configureOutput(outputFunction=logger.debug)


class IlluminationControl:
    """Control an illumination via an attenuator only.

    There is no control over laser power.

    Notes
    -----
    Example configuration::

        default_config = {
            'database': '../power_database.xlsx',
            'index': {
                'name': 'DefaultMicroscope',
                LASER_TAG: 488,
                POWER_TAG: 100},
            'attenuation': {
                'classpath': 'monet.attenuation.KinesisAttenuator',
                'init_kwargs': {
                    'serial': '27257033'}},
            'analysis': {
                'classpath':
                    'monet.analysis.SinusAttenuationCurveAnalyzer',
                'init_kwargs': {
                    'min': 40,
                    'max': 100,
                    'step': 5}}
        }
    """

    def __init__(self, config, do_load_cal=True):
        """Initialize the analyzer and attenuator classes from config.

        Parameters
        ----------
        config : dict
            Keys 'analysis' and 'attenuation', each with sub-keys
            'classpath' and 'init_kwargs'.
        do_load_cal : bool
            Whether to load the latest calibration.
        """
        self.config = config
        self.is_calibrated = False

        self.config = config
        anaconfig = config["analysis"]
        self.analyzer = load_class(
            anaconfig["classpath"], anaconfig["init_kwargs"]
        )

        attconfig = config["attenuation"]
        settgs = attconfig.get("settings", None)
        self.attenuator = load_class(
            attconfig["classpath"], attconfig["init_kwargs"], settgs
        )

        if do_load_cal:
            try:
                self.load_calibration()
            except Exception as e:
                logger.warning(
                    "Could not load calibration: %s. "
                    "Calibration-dependent features disabled.",
                    e,
                )

    @property
    def power(self):
        return self.attenuator.estimate_power()

    @power.setter
    def power(self, power):
        """Set a power level once the power has been calibrated.

        Parameters
        ----------
        power : float
            The power to set in mW.
        """
        if not self.is_calibrated:
            raise ValueError("No calibration present. Please calibrate first.")
        ctrlval = self.analyzer.estimate(power)
        self.set_attenuator(ctrlval)

    def set_attenuator(self, value):
        """Set the attenuator value."""
        self.attenuator.set(value)

    def load_calibration(self, time_idx="latest"):
        """Load a calibration from the database and set the analyzer model.

        Parameters
        ----------
        time_idx : None, 'latest', or list, len 2
            Loads either the latest (if None or a string) or a specific
            date and time.
        """
        fname = self.config["database"]
        cali_pars = io.load_calibration(
            fname, self.config["index"], time_idx=time_idx
        )

        self.analyzer.load_model(cali_pars)
        self.is_calibrated = True

    def disconnect(self):
        """Release the attenuator hardware so its port/handle is freed.

        Call this before re-creating the control on the same hardware
        (e.g. reconnecting in the GUI) so ports are not held open by the
        previous instance. Safe to call more than once.
        """
        release_hardware(getattr(self, "attenuator", None))


class IlluminationLaserControl(IlluminationControl):
    """Control the illumination for multiple lasers and power levels.

    Notes
    -----
    Additional config entry compared to IlluminationControl::

        'lasers': {
            '488': {
                'classpath': 'monet.laser.Toptica',
                'init_kwargs': {'port': 'COM4'}},
            '561': {
                'classpath': 'monet.laser.MPBVFL',
                'init_kwargs': {'port': 'COM7'}},
            '640': {
                'classpath': 'monet.laser.MPBVFL',
                'init_kwargs': {'port': 'COM8'}},
        }
    """

    def __init__(self, config, do_load_cal=True, auto_enable_lasers=True):
        """Initialize the multi-laser illumination control.

        Parameters
        ----------
        config : dict
            Keys 'analysis' and 'attenuation', each with sub-keys
            'classpath' and 'init_kwargs'.
        do_load_cal : bool
            Whether to load the latest calibration.
        auto_enable_lasers : bool
            Whether to switch on lasers at connection.
        """
        super().__init__(config, do_load_cal=do_load_cal)

        # here, all lasers (wavelengths) and powers are loaded
        config["index"][LASER_TAG] = slice(None)
        config["index"][POWER_TAG] = slice(None)

        self.auto_enable_lasers = auto_enable_lasers
        self.lasers = {}
        lasers_missing = []
        for laser_key, lconf in config["lasers"].items():
            try:
                laser = int(laser_key)
            except Exception:
                laser = laser_key
            try:
                settgs = lconf.get("settings", None)
                self.lasers[laser] = load_class(
                    lconf["classpath"], lconf["init_kwargs"], settgs
                )
                # self.lasers[laser].enabled = False
            except Exception as e:
                logger.warning("Could not load laser %s: %s", laser, e)
                print(
                    "Could not load laser {:s}: {:s}. "
                    "Check that the device is on and the COM port / "
                    "connection settings are correct.".format(
                        str(laser), str(e)
                    )
                )
                # Track the original config key — it may differ from `laser`
                # (e.g. the numeric-string key '488' vs the int 488).
                lasers_missing.append(laser_key)

        for laser_key in lasers_missing:
            self.config["lasers"].pop(laser_key)

        if not self.lasers:
            raise RuntimeError(
                "No lasers could be loaded. Configured lasers: {}. "
                "Check that devices are powered on and COM ports have not "
                "changed.".format(list(config["lasers"].keys()))
            )
        self.curr_laser = list(self.lasers.keys())[0]

        # Hard per-laser output-power ceiling (C34 safety envelope). Enforced in
        # this control layer — below *every* actuation path (the HTTP power API,
        # the GUI, the CLI) — so no caller can drive a laser above the limit.
        # Parsed fail-closed: a malformed entry refuses construction rather than
        # silently running without the limit.
        self._max_power = self._parse_safety_envelope(config)

        self._factors = {}  # {laser: transmission_objective}; = P_sample/P_bfp
        self._powermeter_type = {}  # {laser: 'sample'/'bfp'}

        # Where the power meter is *physically* positioned right now. This is
        # independent of the stored calibration's powermeter type: it drives
        # whether a raw reading is projected to the sample plane (BFP) or taken
        # as-is (sample plane) for live measurement and feedback. Defaults to
        # the back focal plane, matching the calibration tab's default.
        self.powermeter_position = POWERMETER_BFP

        if "beampath" in config.keys():
            self.beampath = BeamPath(config["beampath"])
            self.use_beampath = True
        else:
            self.use_beampath = False

        if do_load_cal:
            try:
                self.load_calibration_database()
            except Exception as e:
                logger.warning(
                    "Could not load calibration database: %s. "
                    "Calibration-dependent features disabled.",
                    e,
                )

    def _populate_analyzers(self, db, laser):
        """Create analyzers for various power settings from the database.

        Parameters
        ----------
        db : pandas DataFrame
            The database subset to choose from.
        laser : str or int, numeric
            The laser to choose.

        Returns
        -------
        analyzers : dict
            The analyzers to evaluate the calibrated model for each power
            setting.
        power_ranges : pandas DataFrame
            Indexed by laser power settings, with columns 'min', 'max'.
        """
        if not self.is_calibrated:
            raise KeyError(
                "Cannot populate analyzers: no calibration present."
            )
        laser = int(laser)
        ic(db)
        subdb = db.loc[db.index.get_level_values(LASER_TAG) == laser]
        ic(subdb)
        anaconfig = self.config["analysis"]
        analyzers = {}
        power_ranges = pd.DataFrame(columns=["min", "max"])
        for pwr, cali_pars in subdb.groupby(POWER_TAG):
            pars = {}
            for col in cali_pars.columns:
                val = cali_pars[col].to_numpy()[0]
                try:
                    if not np.isnan(val):
                        pars[col] = val
                except (TypeError, ValueError):
                    pass  # skip non-numeric columns (e.g. powermeter_type)
            analyzers[pwr] = load_class(
                anaconfig["classpath"], anaconfig["init_kwargs"]
            )
            analyzers[pwr].load_model(pars)

            power_ranges.loc[pwr, :] = sorted(analyzers[pwr].output_range())
        ic(power_ranges)
        return analyzers, power_ranges

    @property
    def laser(self):
        """Return the list of laser names present.

        Returns
        -------
        lasers : list of str
            The available laser names.
        """
        return list(self.lasers.keys())

    @laser.setter
    def laser(self, laser):
        """Set the current laser by name.

        Parameters
        ----------
        laser : str
            Must be one of the keys in self.lasers.
        """
        try:
            laser = int(laser)
        except Exception:
            pass
        if laser in self.lasers.keys():
            # self.lasers[self.curr_laser].enabled = False
            self.curr_laser = laser
            self.config["index"][LASER_TAG] = laser
            if self.auto_enable_lasers:
                self.lasers[self.curr_laser].enabled = True
            if self.is_calibrated:
                ic(self.cali_db)
                self._analyzers, self._power_ranges = self._populate_analyzers(
                    self.cali_db, self.curr_laser
                )
                self.laserpower = self._power_ranges.index.min()
            else:
                logger.debug(
                    "Calibration not available, not setting analyzers."
                )
        else:
            # raise KeyError('Laser {:s} is not available'.format(str(laser)))
            print(
                "Laser {:s} is not available".format(str(laser))
                + ". Choose one of "
                + str(list(self.lasers.keys()))
            )

    @property
    def laserpower(self):
        return self.lasers[self.curr_laser].power

    @laserpower.setter
    def laserpower(self, laserpower):
        """Change the laser power output."""
        try:
            laserpower = int(laserpower)
        except Exception:
            pass
        self.curr_laserpower = laserpower
        self.config["index"][POWER_TAG] = laserpower
        self.lasers[self.curr_laser].power = laserpower
        if self.is_calibrated:
            self.analyzer = self._analyzers[self.curr_laserpower]

    @property
    def laser_enabled(self):
        return self.lasers[self.curr_laser].enabled

    @laser_enabled.setter
    def laser_enabled(self, value):
        self.lasers[self.curr_laser].enabled = value

    def _bfp_factor(self, laser=None):
        """Return transmission_objective = P_sample / P_bfp if the latest
        calibration used the back focal plane (BFP) powermeter, else 1.0.

        This is keyed on the *stored calibration* and is used to project the
        calibration model (analyzer output, power ranges) to the sample plane.
        For live power-meter readings use :meth:`_measurement_factor`, which is
        keyed on where the meter is physically positioned now.
        """
        if laser is None:
            laser = self.curr_laser
        pm_type = normalize_powermeter_type(
            self._powermeter_type.get(laser, "sample")
        )
        if pm_type == POWERMETER_BFP:
            return self._factors.get(laser, 1.0)
        return 1.0

    def _measurement_factor(self, laser=None):
        """Objective transmission factor to apply to a *live* meter reading.

        Unlike :meth:`_bfp_factor` (which depends on the stored calibration),
        this depends on where the power meter is *physically* positioned now
        (:attr:`powermeter_position`). A raw reading taken in the back focal
        plane is multiplied by transmission_objective = P_sample / P_bfp to
        project it to the sample plane; a reading taken in the sample plane is
        already a sample-plane power, so the factor is 1.0.

        Returns 1.0 when no transmission factor is known for ``laser`` even
        though the meter is in the BFP — the caller should warn that the
        reported power is the uncorrected raw reading.
        """
        if laser is None:
            laser = self.curr_laser
        pos = normalize_powermeter_type(self.powermeter_position)
        if pos == POWERMETER_BFP:
            return self._factors.get(laser, 1.0)
        return 1.0

    def _has_transmission_factor(self, laser=None):
        """True if an objective transmission factor is loaded for ``laser``."""
        if laser is None:
            laser = self.curr_laser
        return laser in self._factors

    @staticmethod
    def _parse_safety_envelope(config):
        """Parse the per-laser max-power ceiling from config (fail-closed).

        Reads ``config['safety']['max_power_mw']`` — a ``{wavelength: mW}``
        mapping (the interim C34 source until WP-FLEET ships the versioned site
        descriptor). Returns ``{int(laser): float(mW)}``. A malformed entry
        (non-numeric key/value, non-positive or non-finite power, or a
        non-mapping) raises ``ValueError`` so a broken safety config refuses
        construction instead of silently disabling the interlock.
        """
        safety = config.get("safety") or {}
        raw = safety.get("max_power_mw") or {}
        if not isinstance(raw, dict):
            raise ValueError(
                "config 'safety.max_power_mw' must be a mapping of "
                "{laser_wavelength: max_mW}"
            )
        parsed = {}
        for key, val in raw.items():
            try:
                laser = int(key)
            except (TypeError, ValueError):
                raise ValueError(
                    "safety.max_power_mw has a non-numeric laser key: "
                    "{!r}".format(key)
                )
            try:
                mw = float(val)
            except (TypeError, ValueError):
                raise ValueError(
                    "safety.max_power_mw[{!r}] is not a number: {!r}".format(
                        key, val
                    )
                )
            if not np.isfinite(mw) or mw <= 0:
                raise ValueError(
                    "safety.max_power_mw[{!r}] must be a positive, finite "
                    "power in mW, got {!r}".format(key, mw)
                )
            parsed[laser] = mw
        return parsed

    def max_power(self, laser=None):
        """Return the hard per-laser output-power ceiling in mW (C34), or None.

        ``None`` means no ceiling is configured for this laser.

        Parameters
        ----------
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.
        """
        if laser is None:
            laser = self.curr_laser
        try:
            laser = int(laser)
        except (TypeError, ValueError):
            pass
        return self._max_power.get(laser)

    def clamp_to_max_power(self, pwr, laser=None):
        """Clamp a requested sample-plane power to the per-laser ceiling.

        This is the single enforcement point for the C34 safety envelope; it
        sits below every actuation method, so the HTTP power API, the GUI and
        the CLI are all bounded by the same hard limit.

        Parameters
        ----------
        pwr : float
            Requested output power in mW (sample plane).
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.

        Returns
        -------
        (value, was_clamped) : tuple of (float, bool)
            The power to actually use (never above the ceiling) and whether it
            had to be clamped.
        """
        ceiling = self.max_power(laser)
        if ceiling is not None and pwr > ceiling:
            logger.warning(
                "Requested power %.3f mW exceeds the safety ceiling %.3f mW "
                "for laser %s; clamping.",
                pwr,
                ceiling,
                self.curr_laser if laser is None else laser,
            )
            return ceiling, True
        return pwr, False

    def _sample_power_ranges(self, laser=None, power_ranges=None):
        """Return calibrated output power ranges in sample-plane units.

        The analyzers are fitted to power-meter readings, so
        :attr:`_power_ranges` (built from ``analyzer.output_range()``) is in
        BFP units whenever the calibration used the BFP meter. Every power the
        user selects or reads is a sample-plane power, so ranges must be
        projected before they are compared against such a power.

        Parameters
        ----------
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.
        power_ranges : pandas DataFrame, optional
            Ranges to project; defaults to :attr:`_power_ranges`.

        Returns
        -------
        pandas DataFrame
            Indexed by laser power setting, columns 'min' and 'max', in mW in
            the sample plane.
        """
        if power_ranges is None:
            power_ranges = self._power_ranges
        return power_ranges.astype(float) * self._bfp_factor(laser)

    def to_sample_plane(self, raw, laser=None):
        """Project a raw power-meter reading to the sample plane.

        Whether a projection is needed depends on where the meter is
        *physically* positioned now (:attr:`powermeter_position`): a reading
        taken in the back focal plane (BFP) is multiplied by the objective
        transmission factor (P_sample / P_bfp) to become a sample-plane power,
        while a reading taken in the sample plane is returned unchanged.
        Everything we report to the user, write into the MicroManager comment,
        and compare against calibration predictions is in the sample plane.

        Returns `raw` unchanged unless the meter is in the BFP and a
        transmission factor is available for `laser`.

        Parameters
        ----------
        raw : float
            Power-meter reading in its native units (BFP when the meter is in
            the back focal plane).
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.

        Returns
        -------
        float
            Power projected to the sample plane.
        """
        return raw * self._measurement_factor(laser)

    @property
    def power(self):
        attpos = self.attenuator.curr_pos()
        raw = self.analyzer.estimate_power(attpos)
        return raw * self._bfp_factor()

    @power.setter
    def power(self, pwr):
        """Set the power in the sample. If possible with current laser output
        power setting, use this, otherwise change laser output power, and
        in any case, adjust attenuator to get correct sample power.

        Parameters
        ----------
        pwr : float
            Laser power in the sample.
        """
        if not self.is_calibrated:
            raise ValueError("Not calibrated. Cannot set power.")

        # C34 safety ceiling — enforced below the actuation layer.
        pwr, _ = self.clamp_to_max_power(pwr)

        # pwr is a sample-plane power; the calibrated ranges are in
        # power-meter units. Compare both in the sample plane.
        ranges = self._sample_power_ranges()

        newpwr = pwr

        if (
            pwr < ranges.loc[self.curr_laserpower, "min"]
            or pwr > ranges.loc[self.curr_laserpower, "max"]
        ):
            # necessary to change laser output power setting

            # find best laserpwoer: minimal laserpower of which 95% of max
            # is larger than pwr to set
            laserpwr_best = list(ranges.loc[ranges["max"] * 0.95 > pwr].index)
            if len(laserpwr_best) > 0:
                laserpwr_best = min(laserpwr_best)
            else:
                laserpwr_best = max(list(ranges.index))

            if ranges.loc[laserpwr_best, "min"] > pwr:
                newpwr = ranges.loc[laserpwr_best, "min"]
                logger.debug(
                    "Power setting {:.2f} is out of range. ".format(pwr)
                    + "Setting closest power = {:.2f}.".format(newpwr)
                )
                print(
                    "Power setting {:.2f} is out of range. ".format(pwr)
                    + "Setting closest power = {:.2f}.".format(newpwr)
                )
                pwr = newpwr
            elif ranges.loc[laserpwr_best, "max"] < pwr:
                newpwr = ranges.loc[laserpwr_best, "max"]
                logger.debug(
                    "Power setting {:.2f} is out of range. ".format(pwr)
                    + "Setting closest power = {:.2f}.".format(newpwr)
                )
                print(
                    "Power setting {:.2f} is out of range. ".format(pwr)
                    + "Setting closest power = {:.2f}.".format(newpwr)
                )
                pwr = newpwr

            logger.debug(
                "setting laser power to {:s}".format(str(laserpwr_best))
            )
            print("setting laser power to {:s}".format(str(laserpwr_best)))
            self.laserpower = laserpwr_best

        # Apply BFP correction: convert sample-plane target → BFP-equivalent
        # P_bfp = P_sample / transmission_objective, and keep it inside the
        # calibrated model range so a boundary value cannot round out of it.
        raw_lo, raw_hi = sorted(
            self._power_ranges.loc[
                self.curr_laserpower, ["min", "max"]
            ].astype(float)
        )
        newpwr = float(np.clip(newpwr / self._bfp_factor(), raw_lo, raw_hi))
        super(self.__class__, self.__class__).power.__set__(self, newpwr)
        # IlluminationControl.power.fset(self, pwr)

    def set_power_fixed_attenuator(self, pwr, laser=None):
        """Set output power by adjusting laser power, attenuator fixed.

        Uses calibrations at multiple laser power levels. At the current
        attenuator position, each calibration gives an expected output power.
        A linear fit across those (laser_power, output_power) pairs is inverted
        to find the laser power needed to reach *pwr*.

        Parameters
        ----------
        pwr : float
            Desired output power in mW.
        laser : int or str, optional
            Laser wavelength to target; defaults to curr_laser.

        Raises
        ------
        ValueError
            If not calibrated or fewer than 2 laser power levels exist.
        """
        if not self.is_calibrated:
            raise ValueError("Not calibrated. Cannot set power.")

        if laser is None:
            laser = self.curr_laser

        # C34 safety ceiling — enforced below the actuation layer.
        pwr, _ = self.clamp_to_max_power(pwr, laser)

        analyzers, _ = self._populate_analyzers(self.cali_db, laser)
        if len(analyzers) < 2:
            raise ValueError(
                "At least 2 calibrated laser power levels are required for "
                "fixed-attenuator mode."
            )

        att_pos = self.attenuator.curr_pos()

        laser_pwrs = []
        output_pwrs = []
        for lpwr, analyzer in analyzers.items():
            try:
                out = analyzer.estimate_power(att_pos)
                laser_pwrs.append(float(lpwr))
                output_pwrs.append(float(out))
            except Exception:
                pass

        if len(laser_pwrs) < 2:
            raise ValueError(
                "Could not evaluate enough analyzer curves at the current "
                "attenuator position."
            )

        laser_pwrs = np.array(laser_pwrs)
        output_pwrs = np.array(output_pwrs)

        # Linear fit: output ≈ a·laser_pwr + b  →  laser_pwr = (output − b) / a
        a, b = np.polyfit(laser_pwrs, output_pwrs, 1)
        if abs(a) < 1e-12:
            raise ValueError(
                "Linear fit slope is near zero — cannot determine laser power."
            )

        # output_pwrs are in BFP units if BFP-calibrated; convert target
        # P_bfp_target = P_sample / transmission_objective
        factor = self._bfp_factor(laser)
        laser_pwr_needed = float(
            np.clip((pwr / factor - b) / a, laser_pwrs.min(), laser_pwrs.max())
        )
        logger.debug(
            "Fixed-attenuator mode: target %.3f mW → laser power %.3f mW "
            "(fit: a=%.4f, b=%.4f)",
            pwr,
            laser_pwr_needed,
            a,
            b,
        )

        self.lasers[laser].power = laser_pwr_needed

    def set_power_fixed_laser(self, pwr, laser=None):
        """Set output power by adjusting the attenuator only, keeping laser
        power fixed at its current hardware value.

        Uses the calibration curve for the nearest calibrated laser power
        level to estimate the required attenuator position.

        Parameters
        ----------
        pwr : float
            Desired output power in mW.
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.

        Raises
        ------
        ValueError
            If not calibrated.
        """
        if not self.is_calibrated:
            raise ValueError("Not calibrated. Cannot set power.")
        if laser is None:
            laser = self.curr_laser

        # C34 safety ceiling — enforced below the actuation layer.
        pwr, _ = self.clamp_to_max_power(pwr, laser)

        analyzers, _ = self._populate_analyzers(self.cali_db, laser)

        # Find the calibrated level nearest to the current hardware laser power
        curr_lp = float(self.lasers[laser].power)
        closest_level = min(
            analyzers.keys(), key=lambda x: abs(float(x) - curr_lp)
        )
        logger.debug(
            "Fixed-laser mode: using calibrated level %s mW "
            "(hardware laser power %.3f mW)",
            closest_level,
            curr_lp,
        )

        factor = self._bfp_factor(laser)
        raw_lo, raw_hi = sorted(analyzers[closest_level].output_range())
        target = pwr / factor
        span = max(abs(raw_hi - raw_lo), 1e-12)
        if target < raw_lo - 1e-6 * span or target > raw_hi + 1e-6 * span:
            raise ValueError(
                "Requested power {:.3f} mW is outside the range reachable "
                "with the attenuator at laser power {:s}: "
                "{:.3f} – {:.3f} mW.".format(
                    pwr,
                    str(closest_level),
                    raw_lo * factor,
                    raw_hi * factor,
                )
            )
        # clip so a value right at the boundary cannot round out of the
        # calibrated model range
        ctrlval = analyzers[closest_level].estimate(
            float(np.clip(target, raw_lo, raw_hi))
        )
        self.set_attenuator(ctrlval)

    def predict_power_fixed_attenuator(self, laser_pwr, laser=None):
        """Predict output power at the current attenuator position for a given
        laser power, using the same linear interpolation as
        set_power_fixed_attenuator.  Used to compute calibration deviation
        after a fixed-attenuator feedback cycle.

        Parameters
        ----------
        laser_pwr : float
            The laser power currently set (mW).
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.

        Returns
        -------
        float
            Predicted output power in mW.
        """
        if not self.is_calibrated:
            raise ValueError("Not calibrated.")
        if laser is None:
            laser = self.curr_laser

        analyzers, _ = self._populate_analyzers(self.cali_db, laser)
        att_pos = self.attenuator.curr_pos()

        laser_pwrs = []
        output_pwrs = []
        for lpwr, analyzer in analyzers.items():
            try:
                out = analyzer.estimate_power(att_pos)
                laser_pwrs.append(float(lpwr))
                output_pwrs.append(float(out))
            except Exception:
                pass

        if len(laser_pwrs) < 2:
            raise ValueError("Need at least 2 calibrated power levels.")

        a, b = np.polyfit(np.array(laser_pwrs), np.array(output_pwrs), 1)
        raw = float(a * float(laser_pwr) + b)
        return raw * self._bfp_factor(laser)

    def accessible_power_range(self, mode, laser=None):
        """Return the (lo, hi) output power range reachable in the given mode.

        Parameters
        ----------
        mode : str
            One of 'combined' (laser power and attenuator both adjusted),
            'fixed_laser' (laser power fixed, attenuator adjusted) or
            'fixed_attenuator' (attenuator fixed, laser power adjusted).
        laser : int or str, optional
            Laser wavelength; defaults to curr_laser.

        Returns
        -------
        (lo, hi) : tuple of float
            Output power range in mW.

        Raises
        ------
        ValueError
            If not calibrated or no calibration ranges are available.
        """
        if not self.is_calibrated:
            raise ValueError("Not calibrated. Cannot determine power range.")
        if laser is None:
            laser = self.curr_laser

        # _power_ranges only holds the ranges of the instrument's current
        # laser, so populate the analyzers for any other laser asked for.
        try:
            same_laser = int(laser) == int(self.curr_laser)
        except (TypeError, ValueError):
            same_laser = laser == self.curr_laser
        if same_laser:
            pr = getattr(self, "_power_ranges", None)
        else:
            _, pr = self._populate_analyzers(self.cali_db, laser)
        if pr is None or pr.empty:
            raise ValueError("No calibration power ranges available.")
        # power-meter units → sample plane (no-op unless BFP-calibrated)
        pr = self._sample_power_ranges(laser, pr)

        if mode == "combined":
            lo = float(pr["min"].min())
            hi = float(pr["max"].max())
        elif mode == "fixed_laser":
            curr_lp = 0.0
            try:
                curr_lp = float(self.lasers[laser].power)
            except Exception:
                pass
            closest = min(pr.index, key=lambda x: abs(float(x) - curr_lp))
            lo = float(pr.loc[closest, "min"])
            hi = float(pr.loc[closest, "max"])
        elif mode == "fixed_attenuator":
            min_lp = float(pr.index.min())
            max_lp = float(pr.index.max())
            lo = self.predict_power_fixed_attenuator(min_lp, laser)
            hi = self.predict_power_fixed_attenuator(max_lp, laser)
            if lo > hi:
                lo, hi = hi, lo
        else:
            raise ValueError("Unknown power mode '{}'.".format(mode))
        return float(lo), float(hi)

    def load_calibration_database(self):
        load_index = {DEVICE_TAG: self.config["index"][DEVICE_TAG]}
        self.cali_db = io.load_database(
            self.config["database"], load_index, "last combinations"
        )
        logger.debug("loaded latest calibrations for every combination")
        ic(self.cali_db)
        index_combi = self.cali_db.index.to_frame(index=False)
        ic(index_combi)
        ic(self.curr_laser)
        logger.debug(
            index_combi.loc[index_combi[LASER_TAG] == self.curr_laser, :]
        )
        self.curr_laserpower = min(
            index_combi.loc[
                index_combi[LASER_TAG] == self.curr_laser, POWER_TAG
            ]
        )
        self.is_calibrated = True

        # Load powermeter types and correction factors
        self._powermeter_type = {}
        if "powermeter_type" in self.cali_db.columns:
            for laser in self.lasers.keys():
                try:
                    laser_int = int(laser)
                    sub = self.cali_db.loc[
                        self.cali_db.index.get_level_values(LASER_TAG)
                        == laser_int
                    ]
                    if not sub.empty:
                        self._powermeter_type[laser] = (
                            normalize_powermeter_type(
                                sub.iloc[-1]["powermeter_type"]
                            )
                        )
                except Exception:
                    pass

        self._factors = {}
        try:
            device = self.config["index"][DEVICE_TAG]
            factors_df = io.load_factors(
                self.config["database"], device=device
            )
            if not factors_df.empty:
                for laser in self.lasers.keys():
                    try:
                        laser_int = int(laser)
                        sub = factors_df.loc[
                            factors_df.index.get_level_values(LASER_TAG)
                            == laser_int
                        ]
                        if not sub.empty:
                            self._factors[laser] = float(
                                sub.iloc[-1]["transmission_objective_mean"]
                            )
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("Could not load powermeter factors: %s", exc)

        # Populate the analyzers for the current laser, but do NOT switch any
        # laser on: loading the calibration (e.g. on GUI connect) must never
        # start emission — the operator enables lasers explicitly. The
        # auto-enable setting is preserved so intentional set-power actions
        # still turn the laser on.
        prev_auto = self.auto_enable_lasers
        self.auto_enable_lasers = False
        try:
            self.laser = self.curr_laser  # to populate the analyzers
            self.laserpower = self.curr_laserpower
        finally:
            self.auto_enable_lasers = prev_auto

    def record_state(self, laser=None):
        """Persist the current laser power set-point and attenuator position.

        Reads the present values from the hardware and stores them per
        microscope and laser line (see :mod:`monet.hwstate`) so they can be
        restored into the GUI after an unexpected restart. Any failure is
        swallowed — persistence must never break a hardware operation.

        Parameters
        ----------
        laser : int or str, optional
            Laser line to record; defaults to the current laser.
        """
        if laser is None:
            laser = self.curr_laser
        try:
            microscope = self.config["index"][DEVICE_TAG]
        except Exception:
            return
        laser_power = None
        try:
            laser_power = float(self.lasers[laser].power)
        except Exception:
            logger.debug(
                "record_state: reading laser power failed", exc_info=True
            )
        attenuator = None
        try:
            attenuator = float(self.attenuator.curr_pos())
        except Exception:
            logger.debug(
                "record_state: reading attenuator failed", exc_info=True
            )
        try:
            hwstate.save_laser_state(
                microscope,
                laser,
                laser_power=laser_power,
                attenuator=attenuator,
            )
        except Exception:
            logger.debug("record_state: persisting failed", exc_info=True)

    def saved_state(self, laser=None):
        """Return the persisted ``{'laser_power', 'attenuator'}`` for a laser.

        Returns ``None`` if no settings were stored for this microscope/laser.

        Parameters
        ----------
        laser : int or str, optional
            Laser line to look up; defaults to the current laser.
        """
        if laser is None:
            laser = self.curr_laser
        try:
            microscope = self.config["index"][DEVICE_TAG]
        except Exception:
            return None
        try:
            return hwstate.get_laser_state(microscope, laser)
        except Exception:
            logger.debug("saved_state: lookup failed", exc_info=True)
            return None

    def disconnect(self):
        """Switch off and release all lasers and the attenuator.

        Frees the serial ports / SDK sessions held by every laser (and the
        attenuator) so a fresh connection on the same hardware can re-open
        them. Lasers are disabled first so the beam is never left on.
        """
        for laser in list(getattr(self, "lasers", {}).values()):
            try:
                laser.enabled = False
            except Exception:
                logger.debug(
                    "disconnect: disabling laser failed", exc_info=True
                )
            release_hardware(laser)
        super().disconnect()


def run_power_feedback(
    instrument,
    powermeter,
    target_pwr,
    laser,
    mode,
    kp=0.85,
    ki=0.15,
    max_dev_pct=1.0,
    max_iter=20,
    settle_time=2.0,
    progress_callback=None,
    cancel_check=None,
):
    """Closed-loop power setting using a power meter.

    Performs an initial open-loop power set, then iteratively measures the
    output power and corrects it until it is within `max_dev_pct` of
    `target_pwr` or `max_iter` correction iterations have run.

    The power meter and the instrument are passed separately because the
    instrument does not own the power meter (it is held by the calibration
    protocol / CLI shell).

    Parameters
    ----------
    instrument : IlluminationLaserControl
        The calibrated illumination control.
    powermeter : AbstractPowerMeter
        The power meter to read the output power.
    target_pwr : float
        Desired output power in mW.
    laser : int or str
        Laser wavelength to target; must equal instrument.curr_laser so
        that instrument.analyzer is consistent.
    mode : str
        Either 'fixed_laser' (laser power fixed, attenuator adjusted via a
        PI loop) or 'fixed_attenuator' (attenuator fixed, laser power
        adjusted). 'combined' is not supported and raises ValueError.
    kp, ki : float
        Proportional / integral gains, used in 'fixed_laser' mode.
    max_dev_pct : float
        Convergence tolerance, in percent of target_pwr.
    max_iter : int
        Maximum number of correction iterations.
    settle_time : float
        Seconds to wait after the initial set before the first reading.
    progress_callback : callable, optional
        Called as ``progress_callback(iteration, setpoint, measured)``;
        iteration 0 is the reading taken right after the initial set.
    cancel_check : callable, optional
        Returns True to abort the loop early.

    Returns
    -------
    dict
        Dictionary with keys: ``measured`` (float, last measured power
        projected to the sample plane, i.e. raw beampath reading times
        objective transmission factor), ``measured_raw`` (float, the same
        reading before that projection), ``transmission`` (float, the
        objective transmission factor applied; 1.0 if none was used),
        ``converged`` (bool, whether the
        tolerance was reached), ``cali_pred`` (float or None, power the
        calibration predicts), ``out_of_range`` (bool, whether the
        attenuator range limit was hit), ``att_pos`` (float or None, final
        attenuator position), ``laser_pwr`` (float or None, final laser
        power set-point) and ``iterations`` (int, number of correction
        iterations performed).
    """
    import time

    if mode not in ("fixed_laser", "fixed_attenuator"):
        raise ValueError(
            "Feedback is only supported for 'fixed_laser' and "
            "'fixed_attenuator' modes, not '{}'.".format(mode)
        )

    # C34 safety ceiling — clamp the target below the actuation layer so the PI
    # loop's setpoint can never exceed the per-laser limit, whatever the caller
    # passed (no-op if already clamped upstream or no ceiling is configured).
    if hasattr(instrument, "clamp_to_max_power"):
        target_pwr, _ = instrument.clamp_to_max_power(target_pwr, laser)

    # `target_pwr`, the progress callbacks and the returned `measured` are in
    # the sample plane. The loop runs internally in the *physical* meter plane
    # (so meter readings stay native) and projects to the sample plane only at
    # the reporting boundary. `factor` (physical) relates the meter reading to
    # the sample plane and depends on where the meter is positioned now; it is
    # 1.0 unless the meter is in the BFP with a known transmission factor.
    try:
        factor = instrument._measurement_factor(laser)
    except Exception:
        factor = 1.0
    if not factor:
        factor = 1.0
    # `factor_cal` (calibration) relates the *analyzer* output to the sample
    # plane and depends on the stored calibration's powermeter plane. It equals
    # `factor` in the normal case where the meter is where the calibration was
    # taken; the two differ only when the toggle disagrees with the calibration.
    try:
        factor_cal = instrument._bfp_factor(laser)
    except Exception:
        factor_cal = 1.0
    if not factor_cal:
        factor_cal = 1.0
    # Convert a physical-meter-plane value to a calibration-plane value so it
    # can be fed to the analyzer (fitted to calibration-meter readings).
    phys_to_cal = factor / factor_cal
    target_bp = target_pwr / factor  # sample-plane target in meter units

    # Initial open-loop power setting (these take a sample-plane target)
    if mode == "fixed_attenuator":
        instrument.set_power_fixed_attenuator(target_pwr, laser)
    else:  # fixed_laser
        instrument.set_power_fixed_laser(target_pwr, laser)

    time.sleep(settle_time)

    integral_e = 0.0  # accumulated normalized error (PI integral term)
    converged = False
    out_of_range = False
    last_setpoint = target_pwr  # reported in the sample plane

    measured_bp = powermeter.read()
    if progress_callback is not None:
        progress_callback(0, last_setpoint, measured_bp * factor)

    iterations = 0
    for iter_num in range(max_iter):
        if cancel_check is not None and cancel_check():
            break
        dev_pct = (
            abs(measured_bp - target_bp) / target_bp * 100.0
            if target_bp > 0
            else 0.0
        )
        if dev_pct <= max_dev_pct:
            converged = True
            break
        if measured_bp <= 0:
            break  # cannot correct without light
        if mode == "fixed_attenuator":
            # Proportional correction: scale current laser power
            curr_lp = instrument.lasers[laser].power
            instrument.lasers[laser].power = curr_lp * target_bp / measured_bp
            last_setpoint = target_pwr  # output target always target_pwr
        else:
            # PI controller in fixed-laser (attenuator-adjusting) mode.
            # e is the normalised error (dimensionless).
            e = (target_bp - measured_bp) / target_bp
            integral_e += e
            # Anti-windup: bound the integral contribution
            integral_e = float(np.clip(integral_e, -5.0, 5.0))
            corrected_target = target_bp * (1.0 + kp * e + ki * integral_e)
            # Convert to calibration-plane units for the analyzer, which is
            # fitted to calibration-meter readings and whose output_range() is
            # likewise in calibration-plane units.
            corrected_cal = corrected_target * phys_to_cal
            try:
                out_rng = instrument.analyzer.output_range()
                lo = float(out_rng[0])
                hi = float(out_rng[1])
                clamped = float(np.clip(corrected_cal, lo, hi))
                if abs(clamped - corrected_cal) > 1e-9:
                    out_of_range = True
                    integral_e = 0.0  # reset on clamp (anti-windup)
                corrected_cal = clamped
            except Exception:
                corrected_cal = max(0.0, corrected_cal)
            att_pos = instrument.analyzer.estimate(corrected_cal)
            instrument.attenuator.set(att_pos)
            # report sample plane (corrected_cal is in calibration-meter units)
            last_setpoint = corrected_cal * factor_cal
            time.sleep(3)
        time.sleep(0.5)
        measured_bp = powermeter.read(5)
        time.sleep(0.5)
        measured_bp = powermeter.read(50)
        iterations = iter_num + 1
        if progress_callback is not None:
            progress_callback(
                iter_num + 1, last_setpoint, measured_bp * factor
            )

    # Project the final reading to the sample plane for all reporting.
    measured = measured_bp * factor

    # Calibration deviation: what the calibration predicts vs. measured
    cali_pred = None
    try:
        if mode == "fixed_attenuator":
            curr_lp = instrument.lasers[laser].power
            cali_pred = instrument.predict_power_fixed_attenuator(
                curr_lp, laser
            )
        else:
            cali_pred = instrument.power
    except Exception:
        cali_pred = None
    try:
        att_pos = instrument.attenuator.curr_pos()
    except Exception:
        att_pos = None
    try:
        laser_pwr = instrument.lasers[laser].power
    except Exception:
        laser_pwr = None

    return {
        "measured": measured,
        "measured_raw": measured_bp,
        "transmission": factor,
        "converged": converged,
        "cali_pred": cali_pred,
        "out_of_range": out_of_range,
        "att_pos": att_pos,
        "laser_pwr": laser_pwr,
        "iterations": iterations,
    }
