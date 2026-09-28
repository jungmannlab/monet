#!/usr/bin/env python
"""
monet/__init__.py
~~~~~~~~~~~~~~~~~

:authors: Heinrich Grabmayr, 2022
:copyright: Copyright (c) 2022 Jungmann Lab, MPI of Biochemistry
"""

import logging
import os
import warnings
from logging import handlers

import yaml as _yaml

try:
    # Written by setuptools-scm at build/install time (see pyproject.toml
    # [tool.setuptools_scm]). Present in any installed copy.
    from ._version import __version__
except ImportError:  # source tree that was never built/installed, no tag
    __version__ = "unknown"


# configure logger and log that this shouldn't be done here
def config_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s -> %(message)s"
    )
    file_handler = handlers.RotatingFileHandler(
        "monet.log", maxBytes=1e6, backupCount=5
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.WARNING)
    logger.addHandler(file_handler)
    # logger.addHandler(stream_handler)


config_logger()
logger = logging.getLogger(__name__)

# ── per-machine settings via .env ────────────────────────────────────────────
# monet reads its per-machine settings from environment variables: the config /
# protocol path lists (MONET_CONFIG_PATHS / MONET_PROTOCOL_PATHS), the auth
# token(s) (PAINT_MONET_TOKEN / PAINT_MONET_TOKENS) and the auth toggle
# (PAINT_MONET_AUTH). Load a gitignored `.env` (package root, then cwd) into the
# environment so those can live in one file. override=False ⇒ an already-exported
# variable or a systemd EnvironmentFile still wins.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_files(pkg_root=_PKG_ROOT):
    """Load `.env` (package root, then cwd/parents) into os.environ, no override."""
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - python-dotenv is a core dep
        logger.debug("python-dotenv unavailable; .env not loaded")
        return
    try:
        load_dotenv(os.path.join(pkg_root, ".env"), override=False)
        load_dotenv(override=False)
    except Exception:
        logger.debug("could not load .env", exc_info=True)


_load_env_files()

DEVICE_TAG = "name"
LASER_TAG = "wavelength [nm]"
POWER_TAG = "laser_power [mW]"
DATABASE_INDEXLEVELS = [DEVICE_TAG, LASER_TAG, POWER_TAG, "date", "time"]

# Power-meter location (stored per calibration in the
# 'powermeter_type' column):
#   'bfp'    — measured in the back focal plane (BFP) powermeter
#   'sample' — measured manually in the sample plane
# Legacy databases used 'beampath' and 'manual'; normalize_powermeter_type()
# maps those onto the canonical values for backward compatibility.
POWERMETER_BFP = "bfp"
POWERMETER_SAMPLE = "sample"


def normalize_powermeter_type(value):
    """Map a stored/legacy power-meter location onto the canonical value.

    'beampath' (legacy) → 'bfp'; 'manual' (legacy) → 'sample'. Unknown values
    are returned lower-cased and stripped so callers can compare safely.
    """
    v = str(value).strip().lower()
    if v in (POWERMETER_BFP, "beampath", "back_focal_plane", "bfp_powermeter"):
        return POWERMETER_BFP
    if v in (POWERMETER_SAMPLE, "manual", "sample_plane"):
        return POWERMETER_SAMPLE
    return v


# Auth toggle (PAINT_MONET_AUTH): 'off' | 'on' | 'auto' (default). Kept here
# (dependency-light) so both the client (monet.io) and the server helper
# (monet.serviceauth) can read it without importing the FastAPI/auth stack.
#   auto  — enforce iff tokens are configured (backward-compatible default)
#   off   — never enforce (loopback dev); the client omits its token
#   on    — require tokens (serve refuses to start if none are configured)
PAINT_MONET_AUTH_ENV = "PAINT_MONET_AUTH"
_AUTH_OFF_VALUES = frozenset({"off", "0", "false", "no"})
_AUTH_ON_VALUES = frozenset({"on", "1", "true", "yes"})


def auth_mode():
    """Return the normalized auth toggle: 'off', 'on', or 'auto' (default)."""
    value = (os.environ.get(PAINT_MONET_AUTH_ENV) or "").strip().lower()
    if value in _AUTH_OFF_VALUES:
        return "off"
    if value in _AUTH_ON_VALUES:
        return "on"
    return "auto"


def _paths_from_env(var):
    """Parse an os.pathsep-separated path list from ``var``, or None if unset."""
    raw = os.environ.get(var)
    if not raw:
        return None
    return [p.strip() for p in raw.split(os.pathsep) if p.strip()]


###########################################################
#
# Example configurations and protocols are defined in the
# following section.
#
###########################################################

default_config = {
    "database": "../power_database.xlsx",
    "index": {"name": "DefaultMicroscope", LASER_TAG: 488, POWER_TAG: 100},
    "powermeter": {
        "classpath": "monet.powermeter.ThorlabsPowerMeter",
        "init_kwargs": {
            "address": "find connection",
        },
    },
    "attenuation": {
        "classpath": "monet.attenuation.KinesisAttenuator",
        "init_kwargs": {
            "serial": "27257033",
        },
    },
    "analysis": {
        "classpath": "monet.analysis.SinusAttenuationCurveAnalyzer",
        "init_kwargs": {
            "min": 40,
            "max": 100,
            "step": 5,
        },
    },
}


test_config = {
    "database": "power_database.xlsx",
    "index": {"name": "DefaultMicroscope", LASER_TAG: 488, POWER_TAG: 100},
    "powermeter": {
        "classpath": "monet.powermeter.TestPowerMeter",
        "init_kwargs": {
            "address": "find connection",
        },
    },
    "attenuation": {
        "classpath": "monet.attenuation.TestAttenuator",
        "init_kwargs": {
            "bkg": 0,
            "amp": 50,
            "phi": 30,
            "start": 10,
            "step": 5,
        },
    },
    "analysis": {
        "classpath": "monet.analysis.SinusAttenuationCurveAnalyzer",
        "init_kwargs": {
            "min": 30,
            "max": 100,
            "step": 5,
        },
    },
}

calibration_protocol = {
    488: [100, 200, 500, 1000],
    561: [200, 500, 1000, 2000],
    640: [200, 500, 1000, 2000],
}
calibration_protocol = {
    "laser_sequence": [488, 561, 640],
    "laser_powers": {
        488: [100, 200, 500, 1000],
        561: [200, 500, 1000, 2000],
        640: [200, 500, 1000, 2000],
    },
    "beampath": {
        488: {"DC": "Ti488setting", "shutter": True},
        561: {"DC": "Ti561setting", "shutter": True},
        640: {"DC": "Ti640setting", "shutter": True},
        "end": {"DC": "Ti488setting", "shutter": False},
    },
}

test_config_2d = {
    "database": "power_database.xlsx",
    "dest_calibration_plot": "./",
    "index": {
        "name": "DefaultMicroscope",
    },
    "powermeter": {
        "classpath": "monet.powermeter.TestPowerMeter",
        "init_kwargs": {
            "address": "find connection",
        },
    },
    "attenuation": {
        "classpath": "monet.attenuation.TestAttenuator",
        "init_kwargs": {
            "bkg": 0,
            "amp": 50,
            "phi": 30,
            "start": 10,
            "step": 5,
        },
        "analysis": {
            "classpath": "monet.analysis.SinusAttenuationCurveAnalyzer",
            "init_kwargs": {
                "min": 30,
                "max": 100,
                "step": 5,
            },
        },
    },
    "lasers": {
        488: {
            "classpath": "monet.laser.Toptica",
            "init_kwargs": {"port": "COM4"},
        },
        561: {
            "classpath": "monet.laser.MPBVFL",
            "init_kwargs": {"port": "COM7"},
        },
        640: {
            "classpath": "monet.laser.MPBVFL",
            "init_kwargs": {"port": "COM8"},
        },
    },
    "beampath": {
        "DC": {
            "classpath": "monet.beampath.NikonFilterWheel",
            "init_kwargs": {"SN": 1234},
        },
        "shutter": {
            "classpath": "monet.beampath.NikonShutter",
            "init_kwargs": {"SN": 123456},
        },
    },
}

###########################################################
#
# Configs and protocols used by default in the interactive
# command line mode are loaded from default file in the
# following. If this is not possible, the example and test
# protocols defined above are used.
#
###########################################################


# Preferred source: MONET_CONFIG_PATHS / MONET_PROTOCOL_PATHS (from .env / env).
default_config_paths = _paths_from_env("MONET_CONFIG_PATHS")
default_protocol_paths = _paths_from_env("MONET_PROTOCOL_PATHS")

# Legacy fallback: env.yaml (deprecated). Only consulted for a list not already
# supplied via the environment, and it emits a DeprecationWarning so rigs migrate.
if default_config_paths is None or default_protocol_paths is None:
    _legacy_env = None
    try:
        _envpath = os.path.join(_PKG_ROOT, "env.yaml")
        if os.path.exists(_envpath):
            with open(_envpath, "r") as f:
                _legacy_env = _yaml.full_load(f)
    except Exception:
        logger.debug("env.yaml cannot be loaded.", exc_info=True)
        _legacy_env = None
    if _legacy_env:
        warnings.warn(
            "monet: env.yaml is deprecated; set MONET_CONFIG_PATHS and "
            "MONET_PROTOCOL_PATHS in a .env file instead (see .env.template).",
            DeprecationWarning,
            stacklevel=2,
        )
        if default_config_paths is None:
            default_config_paths = _legacy_env.get("config_paths", [])
        if default_protocol_paths is None:
            default_protocol_paths = _legacy_env.get("protocol_paths", [])

default_config_paths = default_config_paths or []
default_protocol_paths = default_protocol_paths or []


CONFIGS = {}
CONFIGS_PATH = ""
PROTOCOLS = {}
PROTOCOLS_PATH = ""

# load configs from file
for defpath in default_config_paths:
    try:
        with open(defpath, "r") as configs_file:
            CONFIGS = _yaml.full_load(configs_file)
        if CONFIGS is not None:
            print("Loaded configurations from " + defpath)
            CONFIGS_PATH = defpath
            break
    except Exception:
        pass
if CONFIGS == {}:
    CONFIGS = {
        "default": default_config,
        "test": test_config,
        "test_2D": test_config_2d,
    }


# load protocols from file
for defpath in default_protocol_paths:
    try:
        with open(defpath, "r") as protocols_file:
            PROTOCOLS = _yaml.full_load(protocols_file)
        if PROTOCOLS is not None:
            print("Loaded protocols from " + defpath)
            PROTOCOLS_PATH = defpath
            break
    except Exception:
        pass
if PROTOCOLS == {}:
    PROTOCOLS = {"test_2D": calibration_protocol}
