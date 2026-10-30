#!/usr/bin/env python
"""
monet/util.py
~~~~~~~~~~~~~

Utility functions for the monet package

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import logging
import os
from importlib import import_module

logger = logging.getLogger(__name__)


def ensure_private_file(path):
    """Create ``path`` owner-only (0600) if missing, else harden it to 0600.

    A secret (a bearer token) must never land in a world-readable file. 0o600
    has no group/other bits, so ``os.open`` with a umask reliably yields 0o600;
    an existing file is chmod-ed down. Shared by the token admin
    (:func:`monet.tokens._write_map`) and the setup wizard
    (:func:`monet.setup_core.write_env`) so the hardening logic lives once.
    Returns ``path``.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    else:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning(
                "could not restrict permissions on %s: %s", path, exc
            )
    return path


def release_hardware(obj, _seen=None):
    """Best-effort release of a hardware object's OS handles.

    Closes serial ports, SDK sessions and motor connections so the device
    can be re-opened by a fresh connection (e.g. when reconnecting in the
    GUI after a laser was switched on). Tries the object's own
    ``stop_polling`` / ``close`` / ``disconnect`` methods and recurses into
    common wrapped handles (``las``, ``laser``, ``lowlvl``, ``device``,
    ``pm``). All errors are swallowed, and ``None`` / simulated ``Test*``
    devices are no-ops.

    Parameters
    ----------
    obj : object or None
        The hardware wrapper to release.
    """
    if obj is None:
        return
    if _seen is None:
        _seen = set()
    if id(obj) in _seen:
        return
    _seen.add(id(obj))

    for name in ("stop_polling", "close", "disconnect"):
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                logger.debug(
                    "release_hardware: %s.%s() failed",
                    type(obj).__name__,
                    name,
                    exc_info=True,
                )
    for attr in ("las", "laser", "lowlvl", "device", "pm"):
        inner = getattr(obj, attr, None)
        if inner is not None and inner is not obj:
            release_hardware(inner, _seen)


def load_class(classpath, init_kwargs={}, settings=None):
    """Load a class by classpath string.

    Parameters
    ----------
    classpath : str
        The path in the package, e.g.
        'monet.attenuation.KinesisAttenuator'.
    init_kwargs : dict
        The first argument to __init__ of the class, being a dict.
    settings : dict
        Keyword arguments to __init__ of the class.
    """
    p, m = classpath.rsplit(".", 1)
    mod = import_module(p)
    Met = getattr(mod, m)
    if settings:
        met = Met(init_kwargs, **settings)
    else:
        met = Met(init_kwargs)
    return met


def update_mm_acquisition_comment(
    laser,
    measured,
    unit,
    att_pos=None,
    laser_pwr=None,
    raw_power=None,
    transmission=None,
    kind="measured",
):
    """Write a power line into the MicroManager acquisition comment.

    Replaces any existing line for this laser so repeated updates don't keep
    appending — and, because the line is keyed only on the wavelength, a
    ``kind="measured"`` update supersedes an earlier ``kind="set"`` line for the
    same laser (and vice versa). Requires the optional `pycromanager` package
    and a running MicroManager instance; if `pycromanager` is not installed this
    is a no-op.

    Parameters
    ----------
    laser : int or str
        Laser wavelength, used to label the comment line.
    measured : float
        The power value to record — the measured power for ``kind="measured"``
        or the requested set-point for ``kind="set"``.
    unit : str
        Power unit string (e.g. 'mW').
    att_pos : float, optional
        Attenuator position to record alongside the power.
    laser_pwr : float, optional
        Laser power set-point to record alongside the power.
    raw_power : float, optional
        Raw power-meter reading before the objective transmission factor was
        applied. Only recorded if `transmission` differs from 1.
    transmission : float, optional
        Objective transmission factor (P_sample / P_bfp) applied to
        `raw_power` to obtain `measured`. A value of 1 or None means no
        factor was used, which is stated explicitly in the comment.
    kind : str, optional
        ``"measured"`` (default) when the power was measured with the meter, or
        ``"set"`` when it was only commanded (not measured). Shown as a
        ``[measured]`` / ``[set]`` tag in the comment.

    Returns
    -------
    str or None
        None on success or if pycromanager is not installed; an error
        string if the update failed.
    """
    import re

    def _replace_or_append(text, pattern, new_str):
        result, count = re.subn(
            r"^" + re.escape(pattern) + r".*$",
            new_str,
            text,
            flags=re.MULTILINE,
        )
        if count == 0:
            result = (
                text
                + ("\n" if text and not text.endswith("\n") else "")
                + new_str
            )
        return result

    pwr_str = "Power {}nm [{}]: {:.3f} {}".format(laser, kind, measured, unit)
    if att_pos is not None:
        pwr_str += " @ att={:.4f}".format(att_pos)
    if laser_pwr is not None:
        pwr_str += " lp={:.1f}mW".format(laser_pwr)
    if transmission is not None and transmission != 1:
        pwr_str += " [BFP reading {:.3f} {} x T_obj={:.4f}]".format(
            raw_power if raw_power is not None else measured / transmission,
            unit,
            transmission,
        )
    else:
        pwr_str += " [no objective transmission factor]"
    # Key only on the wavelength (no colon / tag) so a measured line replaces a
    # previous set line for the same laser, and vice versa.
    pattern = "Power {}nm".format(laser)

    try:
        from pycromanager import Studio

        studio = Studio()
        acqmgr = studio.acquisitions()
        curr_settings = acqmgr.get_acquisition_settings()
        curr_comment = str(curr_settings.comment() or "")
        new_comment = _replace_or_append(curr_comment, pattern, pwr_str)
        new_settings = (
            curr_settings.copy_builder().comment(new_comment).build()
        )
        acqmgr.set_acquisition_settings(new_settings)
        return None  # no error
    except ImportError:
        return None  # pycromanager not installed
    except Exception as exc:
        return str(exc)


def refresh_mm_gui():
    """Make MicroManager re-read the hardware state into its GUI.

    Device changes issued through the pycromanager Core (shutter, filter
    turret, nosepiece, ...) are not reflected in the MicroManager main
    window until its GUI is refreshed. No-op if pycromanager is not
    installed or MicroManager is not running.

    Returns
    -------
    str or None
        None on success or if pycromanager is not installed; an error
        string if the refresh failed.
    """
    try:
        from pycromanager import Studio

        Studio().app().refresh_gui()
        return None
    except ImportError:
        return None  # pycromanager not installed
    except Exception as exc:
        logger.debug("Could not refresh MicroManager GUI: %s", exc)
        return str(exc)


def wavelength_to_rgb(wavelength):
    """Approximate sRGB hex colour of a light wavelength (nm).

    Uses Dan Bruton's piecewise approximation of the visible spectrum
    (~380-780 nm), so a line for e.g. 488 nm renders cyan-blue, 561 nm
    yellow-green and 640 nm red — far more intuitive than an arbitrary palette.
    Wavelengths below 380 are treated as violet and above 780 as deep red so
    UV / IR laser lines still get a sensible, visible colour rather than fading
    to black. Light colours (yellow, green, cyan) are darkened via a luminance
    cap so they stay legible as lines on a white background. Non-numeric input
    falls back to a neutral grey.

    Parameters
    ----------
    wavelength : float
        Wavelength in nanometres.

    Returns
    -------
    str
        ``"#rrggbb"`` colour string.
    """
    try:
        w = float(wavelength)
    except (TypeError, ValueError):
        return "#808080"

    w = min(max(w, 380.0), 780.0)

    if w < 440:
        r, g, b = -(w - 440) / (440 - 380), 0.0, 1.0
    elif w < 490:
        r, g, b = 0.0, (w - 440) / (490 - 440), 1.0
    elif w < 510:
        r, g, b = 0.0, 1.0, -(w - 510) / (510 - 490)
    elif w < 580:
        r, g, b = (w - 510) / (580 - 510), 1.0, 0.0
    elif w < 645:
        r, g, b = 1.0, -(w - 645) / (645 - 580), 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0

    # Dim the extreme violet / red ends where the eye's response falls off.
    if w < 420:
        factor = 0.3 + 0.7 * (w - 380) / (420 - 380)
    elif w > 700:
        factor = 0.3 + 0.7 * (780 - w) / (780 - 700)
    else:
        factor = 1.0

    gamma = 0.8

    def _lin(c):
        if c <= 0:
            return 0.0
        return min(1.0, (c * factor) ** gamma)

    r, g, b = _lin(r), _lin(g), _lin(b)

    # Darken light colours (high perceived luminance) so lines stay legible on
    # a white background — this rescues yellow / yellow-green / cyan, which are
    # otherwise almost invisible, while leaving the already-dark blues, reds and
    # violets untouched.
    max_lum = 0.62
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum > max_lum:
        scale = max_lum / lum
        r, g, b = r * scale, g * scale, b * scale

    def _byte(c):
        return max(0, min(255, int(round(c * 255))))

    return "#{:02x}{:02x}{:02x}".format(_byte(r), _byte(g), _byte(b))
