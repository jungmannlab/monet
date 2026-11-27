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
import math

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

    def __init__(self, config, do_load_cal=True, auto_home=True):
        """Initialize the analyzer and attenuator classes from config.

        Parameters
        ----------
        config : dict
            Keys 'analysis' and 'attenuation', each with sub-keys
            'classpath' and 'init_kwargs'.
        do_load_cal : bool
            Whether to load the latest calibration.
        auto_home : bool
            Whether to home the attenuator once at connection. Homing the
            Thorlabs rotation mount establishes its absolute-position
            reference; without it, absolute moves can be offset. Done here
            in the base ``__init__`` so it runs before any lasers are
            enabled (see ``IlluminationLaserControl``), keeping the
            half-wave-plate power swing dark. A no-op for attenuators
            without a moving axis (AOTF, NI-DAQ analog-out, TestAttenuator).
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

        self.auto_home = auto_home
        if auto_home:
            try:
                self.attenuator.home()
            except Exception as e:
                logger.warning(
                    "Could not home the attenuator at startup: %s. "
                    "Absolute-position accuracy may be reduced until it "
                    "is homed manually.",
                    e,
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
        # reality = pin * model(angle); to reach `power` invert the model at
        # power / pin (pin is 1.0 unless a one-point rescale is active).
        ctrlval = self.analyzer.estimate(power / self._pin_factor())
        self.set_attenuator(ctrlval)

    def _pin_factor(self):
        """One-point-rescale factor for the active calibration (1.0 = none).

        Overridden in the 2-D control to return the per-(laser, power) pin set
        by :meth:`pin_calibration`; the base control is never pinned.
        """
        return 1.0

    def set_attenuator(self, value):
        """Set the attenuator value."""
        self.attenuator.set(value)

    def attenuator_hysteresis_probe(self, read_power, delta=5.0, settle=0.5):
        """Quantify attenuator backlash by re-approaching one angle two ways.

        Re-commands the *current* attenuator angle approached first from below
        (angle - ``delta``) and then from above (angle + ``delta``), reading
        the power after each approach. A rotation-mount / half-wave-plate with
        backlash returns different powers for the two directions even though the
        commanded angle is identical — and because the squared-sine attenuation
        curve is steep, a fraction of a degree of hysteresis becomes a large
        power swing. That makes this the prime suspect for a calibrate-vs-
        measure deviation when both are done in the same plane (where the
        objective transmission factor cancels out).

        The attenuator is returned to the starting angle. Both approaches are
        clamped to the calibrated angle range. ``read_power`` is a zero-arg
        callable returning a power reading (the caller supplies the meter).

        Parameters
        ----------
        read_power : callable
            Zero-arg callable returning a power reading (e.g.
            ``powermeter.read``).
        delta : float
            How far to move away before re-approaching, in attenuator units.
        settle : float
            Seconds to wait after each move before reading.

        Returns
        -------
        dict
            ``target``, ``approach_below``/``approach_above`` (the away
            positions), ``pos_from_below``/``pos_from_above`` (read-back
            angles), ``power_from_below``/``power_from_above``, and
            ``power_spread_frac`` = \\|Δpower\\| / mean power.
        """
        import time

        att = self.attenuator
        target = att.curr_pos()

        # Keep both approaches inside the calibrated angle range.
        params = getattr(self.analyzer, "analysis_parameters", {}) or {}
        lo_lim = params.get("min")
        hi_lim = params.get("max")
        below = target - delta
        above = target + delta
        if lo_lim is not None and np.isfinite(lo_lim):
            below = max(below, lo_lim)
        if hi_lim is not None and np.isfinite(hi_lim):
            above = min(above, hi_lim)

        def _approach(via):
            att.set(via)
            time.sleep(settle)
            att.set(target)
            time.sleep(settle)
            return att.curr_pos(), read_power()

        pos_lo, p_lo = _approach(below)
        pos_hi, p_hi = _approach(above)
        att.set(target)  # leave it where we found it

        mean = (p_lo + p_hi) / 2.0
        spread = abs(p_hi - p_lo) / mean if mean else float("nan")
        result = {
            "target": target,
            "approach_below": below,
            "approach_above": above,
            "pos_from_below": pos_lo,
            "pos_from_above": pos_hi,
            "power_from_below": p_lo,
            "power_from_above": p_hi,
            "power_spread_frac": spread,
        }
        logger.info("attenuator hysteresis probe: %s", result)
        return result

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

        # The stored calibration may have been fit with a different analysis
        # model than the current config (e.g. sinusoidal before switching to
        # polynomial); loading it into the current analyzer then raises. Stay
        # uncalibrated with a clear warning rather than crashing.
        try:
            self.analyzer.load_model(cali_pars)
        except Exception as exc:
            logger.warning(
                "Stored calibration is incompatible with the current "
                "analysis model (%s); recalibrate. Leaving the instrument "
                "uncalibrated.",
                exc,
            )
            self.is_calibrated = False
            return
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

    def __init__(
        self,
        config,
        do_load_cal=True,
        auto_enable_lasers=True,
        auto_home=True,
    ):
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
        auto_home : bool
            Whether to home the attenuator once at connection. Homing
            happens in the base ``__init__`` (before lasers are loaded or
            enabled below), so the power swing occurs with lasers dark.
        """
        super().__init__(config, do_load_cal=do_load_cal, auto_home=auto_home)

        # here, all lasers (wavelengths) and powers are loaded
        config["index"][LASER_TAG] = slice(None)
        config["index"][POWER_TAG] = slice(None)

        self.auto_enable_lasers = auto_enable_lasers
        self.lasers = {}
        # Every configured wavelength, captured BEFORE the failed-to-load ones
        # are pruned from ``self.config["lasers"]`` below. A laser whose COM port
        # is down this session still has a stored calibration and transmission
        # factor; keeping the full set lets the Doctor tab, the manual-factor
        # dialog and auto-compute address it even though its hardware is absent.
        self._configured_lasers = set()
        for laser_key in config["lasers"].keys():
            try:
                self._configured_lasers.add(int(laser_key))
            except Exception:
                pass
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
            # Degraded mode: no laser hardware could be opened (e.g. every COM
            # port is down). Do NOT fail the whole connection — the database
            # inspection (Database tab) and the transmission-factor tools are
            # DB-based and still work, as does the Doctor tab, so the operator
            # can review/compute/set factors for all configured lasers with the
            # hardware absent. Power-setting is the only thing that needs a live
            # laser; curr_laser is None and the power paths guard on it.
            logger.warning(
                "No laser hardware could be loaded (COM ports down?). Running "
                "in degraded mode: database inspection and transmission-factor "
                "tools work; power-setting is disabled until a laser loads. "
                "Configured lasers: %s.",
                sorted(self._configured_lasers),
            )
            self.curr_laser = None
            # No laser to default a power to; keep the attribute defined so a
            # non-GUI caller touching the power path gets a clean guard, not an
            # AttributeError.
            self.curr_laserpower = None
        else:
            self.curr_laser = list(self.lasers.keys())[0]

        # Hard per-laser output-power ceiling (C34 safety envelope). Enforced in
        # this control layer — below *every* actuation path (the HTTP power API,
        # the GUI, the CLI) — so no caller can drive a laser above the limit.
        # Parsed fail-closed: a malformed entry refuses construction rather than
        # silently running without the limit.
        self._max_power = self._parse_safety_envelope(config)

        self._factors = {}  # {laser: transmission_objective}; = P_sample/P_bfp
        self._powermeter_type = {}  # {laser: 'sample'/'bfp'}

        # One-point rescale ("pin") factors keyed by (laser, laser_power): a
        # multiplicative correction applied to the calibration model to track
        # slow laser-power drift between a full calibration and use. Set via
        # pin_calibration(); 1.0 (absent) means the stored calibration is used
        # unchanged. Cleared when a fresh calibration is loaded.
        self._pin_scales = {}

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

    def _analyzers_for(self, laser):
        """Analyzers/power-ranges for ``laser`` under the current model.

        Like :meth:`_populate_analyzers`, but raises a clear error when no
        stored calibration is compatible with the current analysis model
        (e.g. after switching the model before recalibrating) instead of
        letting callers crash on an empty mapping.
        """
        analyzers, power_ranges = self._populate_analyzers(self.cali_db, laser)
        if not analyzers:
            raise ValueError(
                "No calibration compatible with the current analysis model "
                "for laser {}; recalibrate this laser with the current "
                "model.".format(laser)
            )
        return analyzers, power_ranges

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
        if db is None:
            raise KeyError(
                "Cannot populate analyzers: no calibration database loaded."
            )
        laser = int(laser)
        ic(db)
        subdb = db.loc[db.index.get_level_values(LASER_TAG) == laser]
        ic(subdb)
        anaconfig = self.config["analysis"]
        analyzers = {}
        power_ranges = pd.DataFrame(columns=["min", "max"])
        for pwr, cali_pars in subdb.groupby(POWER_TAG):
            # Use the most recent calibration for this power. The DB may hold
            # older rows from a different analysis model (e.g. sinusoidal
            # before switching to polynomial); the latest row matches the
            # current model, and any still-incompatible row is skipped below.
            try:
                cali_pars = cali_pars.sort_index()
            except Exception:
                pass
            row = cali_pars.iloc[-1]
            pars = {}
            for col in cali_pars.columns:
                val = row[col]
                try:
                    if not np.isnan(val):
                        pars[col] = val
                except (TypeError, ValueError):
                    pass  # skip non-numeric columns (e.g. powermeter_type)
            analyzer = load_class(
                anaconfig["classpath"], anaconfig["init_kwargs"]
            )
            try:
                analyzer.load_model(pars)
                # fold in any one-point pin for this (laser, power) so the
                # stored range matches the pinned set/read path
                pin = self._pin_scales.get((laser, pwr), 1.0)
                power_ranges.loc[pwr, :] = sorted(
                    v * pin for v in analyzer.output_range()
                )
            except Exception as exc:
                logger.warning(
                    "Skipping laser power %s: stored calibration is "
                    "incompatible with the current analysis model (%s). "
                    "Recalibrate this power with the current model.",
                    pwr,
                    exc,
                )
                continue
            analyzers[pwr] = analyzer
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
            # Gate on having a database, not on is_calibrated: switching to a
            # laser with a valid calibration must re-establish it even if a
            # previously-selected laser (incompatible with the current model)
            # cleared the flag — otherwise the instrument stays locked out.
            if getattr(self, "cali_db", None) is not None:
                ic(self.cali_db)
                self._analyzers, self._power_ranges = self._populate_analyzers(
                    self.cali_db, self.curr_laser
                )
                if len(self._power_ranges.index) == 0:
                    # No calibration compatible with the current analysis
                    # model (e.g. just after switching the model, before
                    # recalibrating). Stay uncalibrated rather than setting
                    # laserpower to NaN and later KeyError'ing on it.
                    logger.warning(
                        "No calibration compatible with the current analysis "
                        "model for laser %s; recalibrate.",
                        self.curr_laser,
                    )
                    self.is_calibrated = False
                else:
                    self.is_calibrated = True
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
        analyzers = getattr(self, "_analyzers", None)
        if analyzers is None:
            return
        analyzer = analyzers.get(self.curr_laserpower)
        if analyzer is not None:
            self.analyzer = analyzer
            self.is_calibrated = True
        else:
            # No usable calibration for this power — either a new power
            # level or a stored row skipped as incompatible with the
            # current analysis model. Degrade to uncalibrated instead of
            # KeyError'ing: a calibration run must be able to set a power
            # it is about to calibrate. Selecting a power with a loaded
            # calibration restores (rows saved this session need a
            # database reload first). Install a fresh analyzer so a
            # subsequent 1D calibration fits into it instead of mutating
            # another power's stored analyzer in ``_analyzers`` in place.
            if self.is_calibrated:
                logger.warning(
                    "No calibration for laser %s at %s mW; the instrument "
                    "is uncalibrated until a calibrated power is selected "
                    "or this power is calibrated.",
                    self.curr_laser,
                    laserpower,
                )
            self.is_calibrated = False
            anaconfig = self.config["analysis"]
            self.analyzer = load_class(
                anaconfig["classpath"], anaconfig["init_kwargs"]
            )

    @property
    def laser_enabled(self):
        return self.lasers[self.curr_laser].enabled

    @laser_enabled.setter
    def laser_enabled(self, value):
        self.lasers[self.curr_laser].enabled = value

    def _all_laser_keys(self):
        """Laser keys to load factors for: loaded lasers ∪ configured ones.

        A laser whose hardware failed to open this session is absent from
        ``self.lasers`` but still has a stored calibration and transmission
        factor; load those too so the Doctor tab can report every configured
        wavelength, not only the ones that happened to connect. Keys are ints.
        """
        keys = {int(la) for la in self.lasers.keys()}
        keys |= set(getattr(self, "_configured_lasers", set()))
        return keys

    def reload_factors(self):
        """(Re)load the per-laser objective transmission factors from the DB.

        ``self._factors`` is a live cache keyed on laser; it feeds
        :meth:`_bfp_factor` (sample-plane projection) and the power-setting
        path. It is populated at construction via
        :meth:`load_calibration_database`, but the transmission factor is often
        (re)computed *after* that — e.g. the operator pairs a sample-plane and
        a BFP run in the GUI's Database tab. Without a way to refresh this
        cache in place, the running control keeps the stale factor (1.0 when
        none existed yet), so a sample-plane set-point is served at the BFP
        power and under-delivers by the transmission factor. Call this after
        the factor store changes so the correction takes effect without a full
        reconnect. Best-effort: a load failure leaves the cache unchanged-safe
        (empty) rather than raising.

        For any laser the projection store does not cover, fall back to the
        locally-stored factor *pairs* (:func:`io.load_factor_pairs`). An earlier
        monet dropped a failed ``POST /factors`` silently, so a factor computed
        then is missing from the projection store but still present in the local
        pair store — this fallback makes such a calibration usable after the
        upgrade without recomputing it.
        """
        new_factors = {}
        try:
            device = self.config["index"][DEVICE_TAG]
            factors_df = io.load_factors(
                self.config["database"], device=device
            )
            if not factors_df.empty:
                for laser in self._all_laser_keys():
                    try:
                        laser_int = int(laser)
                        sub = factors_df.loc[
                            factors_df.index.get_level_values(LASER_TAG)
                            == laser_int
                        ]
                        if not sub.empty:
                            val = float(
                                sub.iloc[-1]["transmission_objective_mean"]
                            )
                            # Never *apply* a non-physical factor, even one an
                            # older monet persisted before the write-side gate
                            # existed: a garbage value (dark plane -> T≈0 or
                            # T≫1) would drive the sample-plane power-set
                            # (newpwr / factor) to the rails. Drop it (-> no
                            # correction, factor 1.0) and warn.
                            if io.is_physical_transmission(val):
                                new_factors[laser] = val
                            else:
                                logger.warning(
                                    "Ignoring non-physical stored transmission "
                                    "factor %.4g for %s nm (expected %g < T "
                                    "<= %g); using no sample-plane correction. "
                                    "Recompute from clean runs or set it "
                                    "manually.",
                                    val,
                                    laser,
                                    io.MIN_PHYSICAL_TRANSMISSION,
                                    io.MAX_PHYSICAL_TRANSMISSION,
                                )
                    except Exception as exc:
                        logger.debug(
                            "Skipping factor row for %s nm: %s", laser, exc
                        )
            # Only replace the cache on a *successful* load. A successful but
            # empty result legitimately clears it (then pairs fill below).
            self._factors = new_factors
        except Exception as exc:
            # Transient failure (server blip): keep the existing factors rather
            # than wiping a good cache — reload_factors runs at construction and
            # after every Database-tab recompute.
            logger.debug("Could not load powermeter factors: %s", exc)

        self._fill_factors_from_pairs()

    def _fill_factors_from_pairs(self):
        """Fill any missing per-laser factor from the local pair store.

        The projection store (``/factors``) is authoritative when present; this
        only covers lasers it is missing, using the latest-dated pair for that
        wavelength (matching the projection store's ``iloc[-1]`` semantics).
        Best-effort — a connected projection factor is never overwritten.
        """
        missing = [
            la for la in self._all_laser_keys() if la not in self._factors
        ]
        if not missing:
            return
        try:
            device = self.config["index"][DEVICE_TAG]
            pairs = io.load_factor_pairs(
                self.config["database"], device=device
            )
        except Exception as exc:
            logger.debug("Could not load factor pairs: %s", exc)
            return
        if pairs is None or pairs.empty or "factor" not in pairs.columns:
            return
        for laser in missing:
            try:
                wl = float(int(laser))
                sub = pairs.loc[pairs["wavelength"].astype(float) == wl]
                sub = sub.dropna(subset=["factor"])
                if sub.empty:
                    continue
                if "date" in sub.columns:
                    sub = sub.sort_values("date")
                val = float(sub.iloc[-1]["factor"])
                # Same apply-side guard as reload_factors: a non-physical pair
                # factor is never applied (it would corrupt power-setting).
                if not io.is_physical_transmission(val):
                    logger.warning(
                        "Ignoring non-physical transmission factor %.4g for "
                        "%s nm from the local pair store (expected %g < T <= "
                        "%g); using no sample-plane correction.",
                        val,
                        laser,
                        io.MIN_PHYSICAL_TRANSMISSION,
                        io.MAX_PHYSICAL_TRANSMISSION,
                    )
                    continue
                self._factors[laser] = val
                logger.info(
                    "Transmission factor for %s nm recovered from the "
                    "local pair store (%.4f); the projection store had none.",
                    laser,
                    self._factors[laser],
                )
            except Exception as exc:
                logger.debug("Pair fallback for %s nm skipped: %s", laser, exc)

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
        raw = self.analyzer.estimate_power(attpos) * self._pin_factor()
        return raw * self._bfp_factor()

    def _pin_for(self, laser, power):
        """One-point-rescale factor stored for a (laser, power) (1.0 = none)."""
        try:
            key = (int(laser), power)
        except (TypeError, ValueError):
            key = (laser, power)
        return self._pin_scales.get(key, 1.0)

    def _pin_factor(self):
        """Active one-point-rescale factor for the current (laser, power)."""
        return self._pin_for(self.curr_laser, self.curr_laserpower)

    def pin_calibration(self, read_power, settle=0.5, check=True):
        """One-point rescale ("pin") the active calibration to the laser's
        *current* output, correcting slow laser-power drift without a full
        recalibration.

        Measures the actual power at the calibration's highest-power angle and
        stores a multiplicative factor (measured / model-predicted) for the
        current (laser, laser_power), so open-loop set-power and the power
        read-back match reality again. If ``check`` is set, a second point near
        half-power is measured to test whether the drift is a pure amplitude
        rescale. A large residual there (``shape_ok=False``) usually means the
        laser is still warming up — the attenuation curve's small thermal phase
        shift hasn't settled — so warm up ~30 min and retry; rarely the curve
        shape genuinely changed and a full recalibration is needed. In the
        weekend study every line pinned to ~0.5-0.7 %FS once warmed up.

        Parameters
        ----------
        read_power : callable
            Zero-arg callable returning a power-meter reading in the *same
            plane as the calibration model* (the raw meter reading, i.e. not
            projected to the sample plane).
        settle : float
            Seconds to wait after each attenuator move before reading.
        check : bool
            Whether to measure the second (shape-check) point.

        Returns
        -------
        dict
            ``factor``, ``anchor_angle``, ``measured``, ``predicted``; and when
            ``check``: ``check_angle``, ``check_resid_fs`` (% full scale) and
            ``shape_ok`` (False → recommend a full recalibration).
        """
        import time

        if not self.is_calibrated:
            raise ValueError("Not calibrated. Cannot pin.")
        analyzer = self.analyzer
        lo = float(analyzer.analysis_parameters["min"])
        hi = float(analyzer.analysis_parameters["max"])
        grid = np.linspace(lo, hi, 181)
        pmodel = np.asarray(analyzer.estimate_power(grid), dtype=float)
        pmax = float(np.nanmax(pmodel))
        anchor = float(grid[int(np.nanargmax(pmodel))])

        # Restore the attenuator afterwards: a pin is a measurement, not a
        # move the caller asked for.
        entry = self.attenuator.curr_pos()
        try:
            self.set_attenuator(anchor)
            time.sleep(settle)
            measured = float(read_power())
            predicted = float(analyzer.estimate_power(anchor))
            if (
                not (np.isfinite(measured) and np.isfinite(predicted))
                or measured <= 0
                or predicted <= 0
            ):
                raise ValueError(
                    "Pin aborted: implausible reading (measured=%.4g, "
                    "model=%.4g). Is the laser on and the beam routed to the "
                    "meter?" % (measured, predicted)
                )
            factor = measured / predicted
            if not (0.2 <= factor <= 5.0):
                raise ValueError(
                    "Pin aborted: measured/model = %.2f is implausible "
                    "(laser off, wrong beam path, or a stale calibration). "
                    "Recalibrate this line." % factor
                )
            result = {
                "factor": factor,
                "anchor_angle": anchor,
                "measured": measured,
                "predicted": predicted,
            }
            if check:
                half = 0.5 * pmax
                a_chk = float(grid[int(np.nanargmin(np.abs(pmodel - half)))])
                self.set_attenuator(a_chk)
                time.sleep(settle)
                meas2 = float(read_power())
                pred2 = float(analyzer.estimate_power(a_chk))
                fs = (factor * pmax) if (factor * pmax) > 0 else 1.0
                resid = (meas2 - factor * pred2) / fs * 100.0
                result["check_angle"] = a_chk
                result["check_resid_fs"] = resid
                result["shape_ok"] = abs(resid) < 3.0
        finally:
            try:
                self.set_attenuator(entry)
            except Exception:
                pass

        self._pin_scales[(int(self.curr_laser), self.curr_laserpower)] = factor
        # Keep the stored power ranges consistent with the pin so every
        # range-based path (combined-mode clamp, accessible_power_range,
        # _sample_power_ranges, GUI range label) agrees with the pinned set.
        # Recompute from the unpinned model range each time so re-pinning does
        # not compound.
        try:
            self._power_ranges.loc[self.curr_laserpower, :] = sorted(
                v * factor for v in analyzer.output_range()
            )
        except Exception:
            pass
        return result

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

        # Always operate as high as possible on an attenuation curve: pick the
        # LOWEST laser output level that can still reach the target with
        # headroom (its 85%-max exceeds pwr), so a low setpoint is served near
        # the gentle top of a low-power curve instead of the steep,
        # error-amplifying trough of a high-power one. (Previously the level
        # only changed when the target fell outside the *current* level's
        # range, so a small setpoint set right after a large one stayed on the
        # high level's trough.) The 15% headroom (0.85) also keeps combined
        # mode off the attenuator ceiling, so the usual workflow — coarse-set
        # via combined, then switch to fixed-laser feedback — can still raise
        # the power. Fall back to the highest level if none reaches the target.
        reachable = list(ranges.loc[ranges["max"] * 0.85 > pwr].index)
        laserpwr_best = min(reachable) if reachable else max(ranges.index)

        if ranges.loc[laserpwr_best, "min"] > pwr:
            newpwr = ranges.loc[laserpwr_best, "min"]
            msg = "Power setting {:.2f} is out of range. ".format(
                pwr
            ) + "Setting closest power = {:.2f}.".format(newpwr)
            logger.debug(msg)
            print(msg)
            pwr = newpwr
        elif ranges.loc[laserpwr_best, "max"] < pwr:
            newpwr = ranges.loc[laserpwr_best, "max"]
            msg = "Power setting {:.2f} is out of range. ".format(
                pwr
            ) + "Setting closest power = {:.2f}.".format(newpwr)
            logger.debug(msg)
            print(msg)
            pwr = newpwr

        if laserpwr_best != self.curr_laserpower:
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

        analyzers, _ = self._analyzers_for(laser)
        if len(analyzers) < 2:
            raise ValueError(
                "At least 2 calibrated laser power levels are required for "
                "fixed-attenuator mode."
            )

        att_pos = self.attenuator.curr_pos()

        # Each level's model output is scaled by its own pin (1.0 if that level
        # was never pinned), so a one-point pin corrects the level it was
        # measured at and leaves other levels as calibrated — this linear fit
        # across levels therefore reflects only the drift actually measured.
        laser_pwrs = []
        output_pwrs = []
        for lpwr, analyzer in analyzers.items():
            try:
                out = analyzer.estimate_power(att_pos) * self._pin_for(
                    laser, lpwr
                )
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

        analyzers, _ = self._analyzers_for(laser)

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
        pin = self._pin_for(laser, closest_level)
        raw_lo, raw_hi = sorted(analyzers[closest_level].output_range())
        # reality = pin * model; pwr is sample-plane → model-plane target is
        # pwr / transmission / pin
        target = pwr / factor / pin
        span = max(abs(raw_hi - raw_lo), 1e-12)
        if target < raw_lo - 1e-6 * span or target > raw_hi + 1e-6 * span:
            raise ValueError(
                "Requested power {:.3f} mW is outside the range reachable "
                "with the attenuator at laser power {:s}: "
                "{:.3f} – {:.3f} mW.".format(
                    pwr,
                    str(closest_level),
                    raw_lo * factor * pin,
                    raw_hi * factor * pin,
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

        analyzers, _ = self._analyzers_for(laser)
        att_pos = self.attenuator.curr_pos()

        # Each level's model output is scaled by its own pin (1.0 if that level
        # was never pinned), so a one-point pin corrects the level it was
        # measured at and leaves other levels as calibrated — this linear fit
        # across levels therefore reflects only the drift actually measured.
        laser_pwrs = []
        output_pwrs = []
        for lpwr, analyzer in analyzers.items():
            try:
                out = analyzer.estimate_power(att_pos) * self._pin_for(
                    laser, lpwr
                )
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
            _, pr = self._analyzers_for(laser)
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
        if self.curr_laser is None:
            # Degraded mode (no laser hardware): the DB is loaded for inspection
            # and the per-laser factors are refreshed (so the Doctor tab and the
            # transmission-factor tools work), but there is no current laser to
            # pick a default power/analyzers for — skip that and return.
            self.is_calibrated = False
            self._pin_scales = {}
            self.reload_factors()
            return
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
        # a fresh calibration supersedes any one-point pins
        self._pin_scales = {}

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

        self.reload_factors()

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
    settle_time=1.0,
    step_settle=0.8,
    read_settle=0.3,
    read_averaging=30,
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
    step_settle : float
        Seconds to wait for the power to stabilise after each attenuator move
        (fixed-laser mode). The attenuator's ``set`` already blocks until the
        move completes and adds its own ``wait_after_move``; this is the extra
        optical-settling margin on top of that.
    read_settle : float
        Seconds to wait between the move/settle and the correction reading.
    read_averaging : int
        Number of samples the power meter averages for each correction
        reading. Lower is faster but noisier.
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
            # The attenuator's set() already blocks until the move completes
            # (and adds its own wait_after_move); step_settle is the extra
            # optical-settling margin on top of that.
            time.sleep(step_settle)
        time.sleep(read_settle)
        measured_bp = powermeter.read(read_averaging)
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


# Target per-step error decay for a well-tuned fixed-laser loop: each
# correction should cut the remaining error to ~a third. The multiplicative
# P-correction gives an error recursion e_{n+1} ≈ -(m·kp)·e_n + (1-m), whose
# convergence rate is |m·kp|; the suggester scales kp to push the *observed*
# decay ratio toward this target.
_TARGET_DECAY = 0.35


def suggest_pi_gains(
    target,
    measured,
    kp,
    ki,
    mode="fixed_laser",
    converged=False,
    out_of_range=False,
    max_dev_pct=1.0,
    max_iter=20,
):
    """Suggest improved PI gains from a :func:`run_power_feedback` run.

    Analyses the per-iteration measured-power trajectory (the values passed to
    the progress callback, sample plane) to diagnose why the loop behaved as
    it did and propose gains / an iteration cap that should converge faster.

    The fixed-laser loop applies a *multiplicative* correction
    ``setpoint = target·(1 + kp·e + ki·∫e)`` with ``e`` the normalised error,
    so the error decays geometrically at a ratio ``ρ ≈ m·kp`` set by the
    plant/calibration mismatch ``m``. A slowly-decaying (often sign-flipping)
    error that runs out of iterations — the usual "stops halfway" — means ``ρ``
    is too close to 1; the fix is to move ``ρ`` toward :data:`_TARGET_DECAY` by
    rescaling ``kp`` (``kp_new = kp·target/ρ_observed``) and to allow enough
    iterations for that decay to reach tolerance.

    Parameters
    ----------
    target : float
        Sample-plane target power (mW).
    measured : sequence of float
        Measured power per iteration (sample plane); index 0 is the reading
        after the initial open-loop set, matching the progress callbacks.
    kp, ki : float
        The gains used for the analysed run.
    mode : str
        'fixed_laser' (PI on the attenuator) or 'fixed_attenuator'
        (proportional on laser power — gains do not apply).
    converged, out_of_range : bool
        From the run's result dict.
    max_dev_pct : float
        Convergence tolerance used (percent of target).
    max_iter : int
        Iteration cap used.

    Returns
    -------
    dict
        ``kp``, ``ki`` (suggested gains), ``max_iter`` (suggested cap),
        ``changed`` (bool — whether anything differs from the run's settings),
        ``diagnosis`` (short code, see below) and ``message`` (human-readable
        explanation + recommendation). Diagnosis codes: ``converged``,
        ``range_limited``, ``no_light``, ``insufficient``, ``oscillating``,
        ``slow``, ``stalled``.
    """

    def _clamp(v, lo, hi):
        return float(max(lo, min(hi, v)))

    out = {
        "kp": float(kp),
        "ki": float(ki),
        "max_iter": int(max_iter),
        "changed": False,
        "diagnosis": "converged",
        "message": "",
    }

    y = np.asarray(
        [v for v in (measured or []) if v is not None and np.isfinite(v)],
        dtype=float,
    )
    tol = max_dev_pct / 100.0

    if out_of_range:
        out["diagnosis"] = "range_limited"
        out["message"] = (
            "The attenuator hit its range limit, so the target is not "
            "reachable at this laser power — no PI gains can fix that. Change "
            "the laser power (fixed-laser mode) or switch mode. Gains left "
            "unchanged."
        )
        return out
    if target <= 0 or y.size and (y <= 0).all():
        out["diagnosis"] = "no_light"
        out["message"] = (
            "No light was measured during the run — check the shutter, laser "
            "and beam path before tuning gains. Gains left unchanged."
        )
        return out
    if converged:
        # Converged: only nudge if it was slow (spent most of its budget).
        if (y.size - 1) >= 0.7 * max_iter and mode == "fixed_laser":
            out.update(
                kp=_clamp(kp * 0.6, 0.05, 2.0),
                changed=True,
                diagnosis="slow",
                message=(
                    "Converged, but only after {} of {} steps. A lower Kp "
                    "reaches the setpoint in fewer, faster iterations."
                ).format(y.size - 1, max_iter),
            )
            # Ki is tuned independently of Kp — it removes steady-state offset,
            # so it must not be scaled down just because Kp drops (the old
            # ``ki = 0.2·kp`` tie drove Ki toward 0 and reintroduced the very
            # offset the integral term exists to kill). Leave it as set.
        else:
            out["message"] = (
                "Converged in {} step(s) — the gains look fine.".format(
                    max(y.size - 1, 0)
                )
            )
        return out
    if mode != "fixed_laser":
        out["diagnosis"] = "range_limited"
        out["message"] = (
            "Fixed-attenuator mode corrects by scaling the laser power and "
            "uses no PI gains; a run that did not converge is limited by the "
            "laser's power range or linearity, not by Kp/Ki."
        )
        return out
    if y.size < 3:
        out["diagnosis"] = "insufficient"
        out["message"] = (
            "Too few iterations were recorded to analyse — raise the max "
            "iterations or the deviation tolerance and run again."
        )
        out["max_iter"] = int(max(max_iter, 20))
        return out

    # --- fixed-laser, did not converge: analyse the error trajectory ---
    e = (target - y) / target  # signed normalised error per iteration
    a = np.abs(e)
    ratios = [a[i + 1] / a[i] for i in range(a.size - 1) if a[i] > 1e-6]
    rho = float(np.median(ratios)) if ratios else 1.0
    sign_flips = int(np.sum(np.diff(np.sign(e[a > 1e-6])) != 0))

    improved = a[-1] < 0.9 * a[0]
    if rho >= 1.0 and not improved:
        out["diagnosis"] = "stalled" if sign_flips == 0 else "oscillating"
    elif rho >= 1.0:
        out["diagnosis"] = "oscillating"
    else:
        out["diagnosis"] = "slow"

    # Kp and Ki are tuned *independently*, by what the trajectory shows:
    #
    # * A persistent, same-sign residual that stops shrinking ("stalled") is a
    #   steady-state offset — the proportional loop has reached its own
    #   equilibrium below the target. That is an *integral*-action deficiency:
    #   raise Ki to integrate the offset away, and leave Kp (lowering it would
    #   only deepen the proportional offset, which is what the old
    #   ``ki = 0.2·kp`` tie did — it shrank Ki as it shrank Kp, so the loop
    #   crept ever more slowly toward a value it could never reach).
    # * Oscillation or slow decay is a proportional-*rate* problem: scale Kp so
    #   the observed decay ratio rho moves toward the target (the loop rate is
    #   |m·kp|, so a lower Kp both speeds a too-slow decay and damps overshoot),
    #   and leave Ki as the operator set it.
    if out["diagnosis"] == "stalled":
        kp_new = _clamp(kp, 0.05, 2.0)
        # Raise Ki (double it, with a 0.1 floor and a Kp-relative floor) and
        # cap at 1.0. ``max(...)`` already exceeds the current Ki for any
        # Ki < 1.0, so this strictly raises it until it saturates at the cap —
        # without an ``lo > hi`` _clamp that could push Ki past 1.0.
        ki_new = _clamp(max(ki * 2.0, 0.1, 0.25 * kp_new), 0.0, 1.0)
    else:
        # Scale Kp toward the target decay (the loop rate is |m·kp| = rho), but
        # never *raise* it: a loop already decaying fast (rho < target) that
        # merely ran out of iterations needs more steps, not a higher — and
        # destabilising — Kp. Leave Ki exactly as the operator set it.
        kp_new = _clamp(
            min(kp, kp * _TARGET_DECAY / max(rho, 1e-3)), 0.05, 2.0
        )
        ki_new = float(ki)

    # Iterations needed to decay the initial error to tolerance at the target
    # ratio, with a little margin.
    a0 = float(max(a[0], tol * 1.001))

    need = math.ceil(math.log(tol / a0) / math.log(_TARGET_DECAY)) + 3
    max_iter_new = int(_clamp(need, max(max_iter, 20), 60))

    changed = (
        abs(kp_new - kp) > 1e-3
        or abs(ki_new - ki) > 1e-3
        or max_iter_new != max_iter
    )
    out.update(kp=kp_new, ki=ki_new, max_iter=max_iter_new, changed=changed)
    verb = {
        "oscillating": "overshot and oscillated around",
        "stalled": "settled with a steady-state offset short of",
        "slow": "crept slowly toward",
    }[out["diagnosis"]]
    out["message"] = (
        "The loop {} the target (error decay ~{:.0%} per step over {} steps, "
        "{} reversal(s)). Suggested Kp {:.2f} → {:.2f}, Ki {:.3f} → {:.3f}, "
        "max iterations {} → {}."
    ).format(
        verb,
        rho,
        y.size - 1,
        sign_flips,
        kp,
        kp_new,
        ki,
        ki_new,
        max_iter,
        max_iter_new,
    )
    return out


def autotune_pi_gains(
    instrument,
    powermeter,
    target_pwr,
    laser,
    mode="fixed_laser",
    kp=0.85,
    ki=0.15,
    probe_frac=0.15,
    settle_time=0.8,
    read_settle=0.3,
    read_averaging=30,
    confirm=True,
    max_dev_pct=1.0,
    max_iter=20,
    progress_callback=None,
    cancel_check=None,
):
    """Actively identify the plant and derive PI gains (fixed-laser mode).

    Where :func:`suggest_pi_gains` infers gains from one *past* closed-loop
    run — which may not tell the whole story — this runs a deliberate
    open-loop **step test** around the operating point: it commands a few power
    setpoints bracketing ``target_pwr`` (±``probe_frac``), measures the power
    actually delivered at each, and fits the local plant gain
    ``g = d(measured)/d(setpoint)`` that the PI loop really sees. The fixed-laser
    loop is multiplicative, so the *integral* term is what actually corrects the
    gain error; from ``g`` it sets an **integral-led** pair:

    * ``Ki ≈ 0.8/g`` — the primary corrector; and
    * ``Kp ≈ 0.1/g`` — a small damping term.

    This reaches tolerance in ~3 steps (a proportional-heavy tuning adds a slow
    closed-loop pole and crawls); see the derivation at the gain assignment.

    With ``confirm`` it then runs a short :func:`run_power_feedback` with the
    derived gains and reports whether it converged. The hardware is left at the
    open-loop ``target_pwr`` setting (or at the confirmed closed-loop point).

    Only ``fixed_laser`` mode has PI gains to tune; ``fixed_attenuator``
    corrects by scaling the laser power and is rejected.

    Parameters mirror :func:`run_power_feedback` where they overlap. ``kp`` /
    ``ki`` are the *current* gains, returned unchanged if the step test cannot
    be completed (no light, degenerate/clamped probe span, non-physical slope).

    Returns
    -------
    dict
        ``kp``, ``ki``, ``max_iter`` (derived gains / cap), ``plant_gain``
        (float or None), ``linearity`` (0..1, |corr| of the probe fit, or
        None), ``confirmed`` (bool — whether a confirmation run was done),
        ``converged`` (bool or None), ``measured`` (float or None, sample
        plane), ``changed`` (bool), ``diagnosis`` (``tuned`` / ``no_light`` /
        ``degenerate`` / ``cancelled`` / ``unsupported``), ``message`` and
        ``trajectory`` (list of ``(label, setpoint, measured)``, sample plane).
    """
    import time

    out = {
        "kp": float(kp),
        "ki": float(ki),
        "max_iter": int(max_iter),
        "plant_gain": None,
        "linearity": None,
        "confirmed": False,
        "converged": None,
        "measured": None,
        "changed": False,
        "diagnosis": "tuned",
        "message": "",
        "trajectory": [],
    }

    if mode != "fixed_laser":
        out["diagnosis"] = "unsupported"
        out["message"] = (
            "Auto-tune applies only to fixed-laser mode (the attenuator-"
            "adjusting PI loop). Fixed-attenuator mode scales the laser power "
            "and uses no Kp/Ki."
        )
        return out
    if not getattr(instrument, "is_calibrated", False):
        raise ValueError("Not calibrated. Cannot auto-tune.")

    # Plane / unit bookkeeping, mirroring run_power_feedback: the loop runs in
    # the physical meter plane and the analyzer works in the calibration plane.
    try:
        factor = instrument._measurement_factor(laser) or 1.0
    except Exception:
        factor = 1.0
    try:
        factor_cal = instrument._bfp_factor(laser) or 1.0
    except Exception:
        factor_cal = 1.0
    phys_to_cal = factor / factor_cal if factor_cal else 1.0

    if hasattr(instrument, "clamp_to_max_power"):
        target_pwr, _ = instrument.clamp_to_max_power(target_pwr, laser)
    target_bp = target_pwr / factor if factor else target_pwr

    try:
        out_rng = instrument.analyzer.output_range()
        lo = float(min(out_rng))
        hi = float(max(out_rng))
    except Exception:
        lo, hi = 0.0, float("inf")

    frac = float(np.clip(probe_frac, 0.02, 0.5))
    # Three points (−frac, nominal, +frac) give both a slope and a linearity
    # check; clamping to the analyzer range keeps every probe reachable.
    probe_targets = [
        target_bp * (1.0 - frac),
        target_bp,
        target_bp * (1.0 + frac),
    ]

    def _restore_target():
        # Always leave the hardware at the requested open-loop target, never at
        # a probe setpoint (a probe can sit ±probe_frac off the target). Called
        # on every post-probe exit, including the early returns and cancel.
        try:
            instrument.set_power_fixed_laser(target_pwr, laser)
            time.sleep(settle_time)
        except Exception as exc:
            logger.debug("autotune: restoring target failed: %s", exc)

    pts = []  # (commanded meter-plane setpoint, measured meter-plane power)
    cancelled = False
    for idx, sp_bp in enumerate(probe_targets):
        if cancel_check is not None and cancel_check():
            cancelled = True
            break
        sp_cal = float(np.clip(sp_bp * phys_to_cal, lo, hi))
        try:
            att = instrument.analyzer.estimate(sp_cal)
            instrument.attenuator.set(att)
        except Exception as exc:
            logger.warning("autotune: could not set probe %d: %s", idx, exc)
            continue
        time.sleep(settle_time)
        time.sleep(read_settle)
        meas_bp = powermeter.read(read_averaging)
        sp_bp_cmd = sp_cal / phys_to_cal if phys_to_cal else sp_cal
        pts.append((sp_bp_cmd, meas_bp))
        out["trajectory"].append(
            ("probe {}".format(idx + 1), sp_bp_cmd * factor, meas_bp * factor)
        )
        if progress_callback is not None:
            progress_callback(idx, sp_bp_cmd * factor, meas_bp * factor)

    if cancelled:
        _restore_target()
        out["diagnosis"] = "cancelled"
        out["message"] = "Auto-tune cancelled before the step test finished."
        return out

    valid = [(s, m) for (s, m) in pts if s > 0 and m > 0]
    span_ok = len(valid) >= 2 and abs(valid[-1][0] - valid[0][0]) > 1e-9
    if not valid or all(m <= 0 for _, m in pts):
        _restore_target()
        out["diagnosis"] = "no_light"
        out["message"] = (
            "No light was measured during the step test — check the shutter, "
            "laser and beam path. Gains left unchanged."
        )
        return out
    if not span_ok:
        _restore_target()
        out["diagnosis"] = "degenerate"
        out["message"] = (
            "The probe setpoints collapsed to one point (likely the attenuator "
            "range limit at this laser power), so the plant gain could not be "
            "measured. Change the laser power or target. Gains left unchanged."
        )
        return out

    s_arr = np.array([s for s, _ in valid], dtype=float)
    m_arr = np.array([m for _, m in valid], dtype=float)
    g, _b = np.polyfit(s_arr, m_arr, 1)
    if not np.isfinite(g) or g <= 0:
        _restore_target()
        out["diagnosis"] = "degenerate"
        out["message"] = (
            "The step test produced a non-physical plant gain "
            "({:.3g}); gains left unchanged. Check for saturation or a "
            "stuck attenuator.".format(float(g))
        )
        return out

    # |correlation| of the probe points as a crude linearity score. Needs at
    # least 3 distinct points to be meaningful — two points are collinear by
    # definition (|corr| == 1), which would advertise a false "perfect" score.
    if s_arr.size >= 3 and np.std(s_arr) > 0 and np.std(m_arr) > 0:
        linearity = float(abs(np.corrcoef(s_arr, m_arr)[0, 1]))
    else:
        linearity = None

    # The fixed-laser loop is *multiplicative* — corrected = target*(1 + kp*e +
    # ki*I) and measured = g*corrected — so the INTEGRAL term is the primary
    # corrector of the plant/calibration gain error g (its steady state is
    # ki*I* = 1/g - 1). A large Kp does not speed that up; it adds a slow
    # positive closed-loop pole (the loop poles are the roots of
    # λ² - (1 - g(kp+ki))λ - g·kp, whose product is -g·kp). The old
    # proportional-heavy tuning (kp = 0.35/g, ki = 0.4·kp) put that pole near
    # 0.9 and crawled to target in ~25 steps. Place the loop *integral-led*
    # instead: ki ≈ 0.8/g drives the correction, a small kp ≈ 0.1/g damps it.
    # That reaches 1% in ~3 steps and stays robust to a ±30% error in the
    # measured g (both verified by simulation and by test_autotune_* against the
    # real loop). g*kp = 0.1 and g*ki = 0.8 are gain-invariant by construction,
    # so the dynamics are the same whatever plant gain the step test finds.
    kp_new = float(np.clip(0.10 / g, 0.01, 2.0))
    ki_new = float(np.clip(0.80 / g, 0.02, 2.0))
    max_iter_new = int(max(max_iter, 20))

    out.update(
        kp=kp_new,
        ki=ki_new,
        max_iter=max_iter_new,
        plant_gain=float(g),
        linearity=linearity,
        changed=(
            abs(kp_new - kp) > 1e-3
            or abs(ki_new - ki) > 1e-3
            or max_iter_new != max_iter
        ),
    )

    # Leave the hardware at the requested target (open-loop) rather than at the
    # last probe point before any confirmation run.
    _restore_target()

    if confirm and not (cancel_check is not None and cancel_check()):
        res = run_power_feedback(
            instrument,
            powermeter,
            target_pwr,
            laser,
            "fixed_laser",
            kp=kp_new,
            ki=ki_new,
            max_dev_pct=max_dev_pct,
            max_iter=max_iter_new,
            settle_time=settle_time,
            read_settle=read_settle,
            read_averaging=read_averaging,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        out["confirmed"] = True
        out["converged"] = bool(res.get("converged"))
        out["measured"] = res.get("measured")

    lin_txt = (
        "" if linearity is None else " (linearity {:.2f})".format(linearity)
    )
    confirm_txt = ""
    if out["confirmed"]:
        confirm_txt = " Confirmation run {}.".format(
            "converged" if out["converged"] else "did not converge"
        )
    out["message"] = (
        "Step test measured plant gain g = {:.3f}{}. Derived integral-led Kp "
        "{:.2f} → {:.2f}, Ki {:.3f} → {:.3f}, max iterations {} → {}.{}"
    ).format(
        g,
        lin_txt,
        kp,
        kp_new,
        ki,
        ki_new,
        max_iter,
        max_iter_new,
        confirm_txt,
    )
    return out
