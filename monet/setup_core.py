"""
monet/setup_core.py
~~~~~~~~~~~~~~~~~~~~

Headless engine behind the ``monet setup`` wizard (CLI + GUI). It orchestrates
the primitives that already exist elsewhere in the package so the two front-ends
share one implementation:

* state detection (is this machine configured yet?) — :func:`detect_state`;
* reading/writing the per-machine ``.env`` (config/protocol paths, server URL
  token, auth toggle) — :func:`read_env` / :func:`write_env`;
* a registry of the hardware driver classes and their ``init_kwargs``
  (:data:`DRIVERS`) plus best-effort device enumeration, so a wizard can offer
  concrete choices when authoring a microscope config;
* assembling and persisting a ``configs.yaml`` microscope entry (and a
  ``protocols.yaml`` sweep) — :func:`build_microscope_entry` /
  :func:`write_microscope_config` / :func:`write_protocol_entry`, including
  scaffolding the ``docs/examples`` templates into an empty central folder;
* a no-actuation connection check for a just-authored config
  (:func:`test_microscope`);
* server-side orchestration helpers — :func:`guided_server_instructions` and
  :func:`parse_token_line` (the live SSH install lives in
  :func:`remote_install_server`).

This module deliberately imports **no** Qt and **no** vendor SDKs at import
time; everything hardware/GUI-specific is lazy so it loads on a bare client.

:authors: Heinrich Grabmayr, 2026
:copyright: Copyright (c) 2026 Jungmann Lab, MPI of Biochemistry
"""

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Optional

import monet
from monet import _PKG_ROOT

logger = logging.getLogger(__name__)

# The keys the wizard manages in the .env (same names monet reads at import).
ENV_CONFIG_PATHS = "MONET_CONFIG_PATHS"
ENV_PROTOCOL_PATHS = "MONET_PROTOCOL_PATHS"
ENV_CLIENT_TOKEN = "PAINT_MONET_TOKEN"
ENV_AUTH = "PAINT_MONET_AUTH"

# Server-side install knowledge, kept in one place so the guided text and the
# SSH automation agree and the coupling to deploy/setup-server.sh (which owns
# these defaults) is explicit. If setup-server.sh changes its ETC_DIR or the
# repo's canonical ref/location, update these to match.
SERVER_ENV_FILE = "/etc/monet/monet.env"
DEFAULT_GIT_REF = "master"
SETUP_SCRIPT_URL = (
    "https://raw.githubusercontent.com/jungmannlab/monet/"
    "{ref}/deploy/setup-server.sh"
)


# ──────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────
def user_config_dir():
    """Persistent per-user monet directory for a standalone/frozen install.

    Honours ``MONET_HOME`` (set by the bundled Windows launcher so the whole
    app agrees); otherwise ``%LOCALAPPDATA%\\monet`` on Windows or ``~/.monet``
    elsewhere. Only used on a frozen build — a normal install keeps state beside
    the package.
    """
    base = os.environ.get("MONET_HOME")
    if base:
        return base
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "monet" if os.name == "nt" else ".monet")


def default_env_path():
    """The ``.env`` monet loads at import.

    A normal (editable/wheel) install keeps it beside the package, the same file
    :func:`monet.tokens._default_env_file` targets, so what the wizard writes is
    exactly what monet reads next run. A **frozen** PyInstaller build's package
    dir is an ephemeral temp dir (``sys._MEIPASS``), so there the ``.env`` is
    persisted under :func:`user_config_dir` instead (the launcher chdirs there,
    so monet's import-time ``.env`` search finds it too).
    """
    if getattr(sys, "frozen", False):
        return os.path.join(user_config_dir(), ".env")
    return os.path.join(_PKG_ROOT, ".env")


def examples_dir():
    """Directory holding the shipped ``configs.yaml`` / ``protocols.yaml``."""
    return os.path.join(_PKG_ROOT, "docs", "examples")


# ──────────────────────────────────────────────────────────────────────────
# State detection
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class SetupState:
    """A snapshot of how configured this machine is, for the wizard."""

    env_path: str
    env_exists: bool
    configs_path: str  # monet.CONFIGS_PATH ('' == built-in configs only)
    configs_loaded: bool
    server_url: Optional[str]
    server_status: Optional[dict]  # io.check_server_auth() verdict, or None

    @property
    def configured(self):
        """True once an external config loads or a populated .env exists."""
        return self.configs_loaded or self.env_exists

    @property
    def first_run(self):
        """True when nothing is set up — the auto-launch trigger."""
        return not self.configs_loaded and not self.env_exists


def _first_server_url():
    """Return the first ``http(s)://`` database URL across loaded configs."""
    from monet.io import _is_server_url

    configs = getattr(monet, "CONFIGS", None) or {}
    for cfg in configs.values():
        if not isinstance(cfg, dict):
            continue
        db = cfg.get("database")
        if isinstance(db, str) and _is_server_url(db):
            return db.rstrip("/")
    return None


def detect_state(probe_server=True):
    """Inspect the machine and return a :class:`SetupState`.

    ``probe_server`` hits the configured server's ``/health`` + ``/auth/whoami``
    via :func:`monet.io.check_server_auth`; pass ``False`` to skip the network
    call (e.g. when only the local configuration matters).
    """
    env_path = default_env_path()
    env_exists = os.path.isfile(env_path) and os.path.getsize(env_path) > 0

    configs_path = getattr(monet, "CONFIGS_PATH", "") or ""
    configs_loaded = bool(configs_path)

    server_url = _first_server_url()
    server_status = None
    if server_url and probe_server:
        try:
            from monet import io

            server_status = io.check_server_auth(server_url)
        # pragma: no cover - never let a probe crash detection
        except Exception:
            logger.debug("server probe failed", exc_info=True)
            server_status = None

    return SetupState(
        env_path=env_path,
        env_exists=env_exists,
        configs_path=configs_path,
        configs_loaded=configs_loaded,
        server_url=server_url,
        server_status=server_status,
    )


# ──────────────────────────────────────────────────────────────────────────
# .env read / write  (0600, reusing the tokens.py idiom)
# ──────────────────────────────────────────────────────────────────────────
def read_env(env_path=None):
    """Return the ``.env`` as a plain dict (``{}`` if it does not exist)."""
    env_path = env_path or default_env_path()
    if not os.path.isfile(env_path):
        return {}
    from dotenv import dotenv_values

    return {k: v for k, v in dotenv_values(env_path).items() if v is not None}


def write_env(updates, env_path=None):
    """Write ``updates`` (``{KEY: value}``) into the ``.env`` at 0600.

    Edits in place (keeps unrelated keys/comments), reflects values into the
    live ``os.environ`` so the running process sees them immediately, and
    returns the path written. ``None`` values are skipped (not deleted). The
    file is created/hardened to 0600 first (shared with the token admin via
    :func:`monet.util.ensure_private_file`) because it holds the bearer token.
    """
    env_path = env_path or default_env_path()
    from monet.util import ensure_private_file

    ensure_private_file(env_path)
    from dotenv import set_key

    for key, value in updates.items():
        if value is None:
            continue
        set_key(env_path, key, str(value), quote_mode="never")
        os.environ[key] = str(value)
    return env_path


def paths_to_env(paths):
    """Join a list of file paths into an ``os.pathsep`` env-var string."""
    return os.pathsep.join(str(p) for p in paths if p)


# ──────────────────────────────────────────────────────────────────────────
# Server probing (thin re-exports so front-ends import one module)
# ──────────────────────────────────────────────────────────────────────────
def check_server(url):
    """Probe reachability + auth for ``url`` (see io.check_server_auth)."""
    from monet import io

    return io.check_server_auth(url.rstrip("/"))


# ──────────────────────────────────────────────────────────────────────────
# Hardware driver registry
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Field:
    """One ``init_kwargs`` field of a driver, for a form/prompt."""

    name: str
    type: str = "str"  # str | int | float | bool | choice | dict
    default: object = None
    required: bool = False
    help: str = ""
    choices_from: Optional[str] = None  # 'serial' | 'visa' | 'nidaq'


@dataclass
class Driver:
    """A selectable driver class and the fields the wizard should collect."""

    classpath: str
    label: str
    needs_sdk: bool = False  # lazy-imports a vendor SDK (fails w/o hardware)
    fields: list = field(default_factory=list)


# Ground-truth from the driver source (not the partly-stale ONBOARDING tables).
DRIVERS = {
    "powermeter": {
        "TestPowerMeter": Driver(
            classpath="monet.powermeter.TestPowerMeter",
            label="Simulated power meter (no hardware)",
            fields=[
                Field("address", default="find connection"),
                Field("bkg", "float", 0),
                Field("amp", "float", 50),
                Field("phi", "float", 30),
                Field("start", "float", 10),
                Field("step", "float", 5),
                Field("noise", "float", 0.5),
            ],
        ),
        "ThorlabsPowerMeter": Driver(
            classpath="monet.powermeter.ThorlabsPowerMeter",
            label="Thorlabs PM100D (legacy VISA/USBTMC driver)",
            needs_sdk=True,
            fields=[
                Field(
                    "address",
                    default="find connection",
                    help="VISA resource, or 'find connection' to auto-detect "
                    "the first Thorlabs meter",
                    choices_from="visa",
                ),
            ],
        ),
        "ThorlabsTLPMPowerMeter": Driver(
            classpath="monet.powermeter.ThorlabsTLPMPowerMeter",
            label="Thorlabs TLPM meter (PM100D2 / Optical Power Monitor)",
            needs_sdk=True,
            fields=[
                Field("address", default="find connection"),
                Field(
                    "dll_path",
                    default="",
                    help="folder with TLPM_64.dll if not on PATH (Windows)",
                ),
            ],
        ),
    },
    "attenuation": {
        "TestAttenuator": Driver(
            classpath="monet.attenuation.TestAttenuator",
            label="Simulated attenuator (no hardware)",
            fields=[
                Field("bkg", "float", 0),
                Field("amp", "float", 50),
                Field("phi", "float", 30),
                Field("start", "float", 10),
                Field("step", "float", 5),
            ],
        ),
        "KinesisAttenuator": Driver(
            classpath="monet.attenuation.KinesisAttenuator",
            label="Thorlabs Kinesis rotation mount (half-wave plate)",
            needs_sdk=True,
            fields=[
                Field(
                    "serial",
                    required=True,
                    help="serial number printed on the Kinesis cube",
                ),
            ],
        ),
        "AAAOTFAttenuator": Driver(
            classpath="monet.attenuation.AAAOTFAttenuator",
            label="AA Opto-Electronic AOTF (serial)",
            fields=[
                Field("port", default="COM10", choices_from="serial"),
                Field(
                    "channeldef_loc",
                    required=True,
                    help="CSV mapping wavelength -> (channel, frequency)",
                ),
            ],
        ),
        "NIdaqmxAOAttenuator": Driver(
            classpath="monet.attenuation.NIdaqmxAOAttenuator",
            label="NI-DAQ analog-out AOTF",
            needs_sdk=True,
            fields=[
                Field(
                    "lines",
                    "dict",
                    required=True,
                    help="wavelength -> AO line, e.g. {488: 'Dev1/ao0'}",
                    choices_from="nidaq",
                ),
            ],
        ),
    },
    "analysis": {
        "SinusAttenuationCurveAnalyzer": Driver(
            classpath="monet.analysis.SinusAttenuationCurveAnalyzer",
            label="Sinusoidal (half-wave plate rotation)",
            fields=[
                Field("min", "float", 0),
                Field("max", "float", 180),
                Field("step", "float", 5),
            ],
        ),
        "LinearCurveAnalyzer": Driver(
            classpath="monet.analysis.LinearCurveAnalyzer",
            label="Linear",
            fields=[
                Field("min", "float", 0),
                Field("max", "float", 100),
                Field("step", "float", 5),
            ],
        ),
        "PolynomAttenuationCurveAnalyzer": Driver(
            classpath="monet.analysis.PolynomAttenuationCurveAnalyzer",
            label="Polynomial (AOTF voltage)",
            fields=[
                Field("min", "float", 0),
                Field("max", "float", 100),
                Field("step", "float", 5),
                Field("polydegree", "int", 10),
            ],
        ),
        "PointCurveAnalyzer": Driver(
            classpath="monet.analysis.PointCurveAnalyzer",
            label="Single point (constant)",
            fields=[],
        ),
        "SplineAttenuationCurveAnalyzer": Driver(
            classpath="monet.analysis.SplineAttenuationCurveAnalyzer",
            label="Smoothing spline",
            fields=[
                Field("min", "float", 0),
                Field("max", "float", 100),
                Field("step", "float", 5),
                Field("smoothing", "float", default=None),
            ],
        ),
    },
    "laser": {
        "TestLaser": Driver(
            classpath="monet.laser.TestLaser",
            label="Simulated laser (no hardware)",
            fields=[],
        ),
        "MPBVFL": Driver(
            classpath="monet.laser.MPBVFL",
            label="MPB VFL",
            fields=[Field("port", required=True, choices_from="serial")],
        ),
        "Toptica": Driver(
            classpath="monet.laser.Toptica",
            label="Toptica iBeam",
            fields=[Field("port", required=True, choices_from="serial")],
        ),
        "LaserQuantum": Driver(
            classpath="monet.laser.LaserQuantum",
            label="Laser Quantum",
            fields=[Field("port", required=True, choices_from="serial")],
        ),
        "Cobolt": Driver(
            classpath="monet.laser.Cobolt",
            label="Cobolt (keyed)",
            needs_sdk=True,
            fields=[
                Field("port", default="", choices_from="serial"),
                Field("serialnumber", default=""),
                Field("baudrate", "int", 115200),
            ],
        ),
        "Cobolt_OEM": Driver(
            classpath="monet.laser.Cobolt_OEM",
            label="Cobolt OEM (keyless)",
            needs_sdk=True,
            fields=[
                Field("port", default="", choices_from="serial"),
                Field("serialnumber", default=""),
                Field("baudrate", "int", 115200),
            ],
        ),
    },
    "beampath": {
        "TestShutter": Driver(
            classpath="monet.beampath.TestShutter",
            label="Simulated beam-path device (no hardware)",
            fields=[],
        ),
        "NikonShutter": Driver(
            classpath="monet.beampath.NikonShutter",
            label="Micro-Manager Core shutter",
            needs_sdk=True,
            fields=[],
        ),
        "NikonFilterWheel": Driver(
            classpath="monet.beampath.NikonFilterWheel",
            label="Micro-Manager 'Filter turret' config group",
            needs_sdk=True,
            fields=[],
        ),
        "NikonNosepiece": Driver(
            classpath="monet.beampath.NikonNosepiece",
            label="Micro-Manager 'Nosepiece' config group",
            needs_sdk=True,
            fields=[],
        ),
    },
}


def classpath_for(category, class_name):
    """Return the full classpath for a registry entry (KeyError if unknown)."""
    return DRIVERS[category][class_name].classpath


# ──────────────────────────────────────────────────────────────────────────
# Best-effort device enumeration (return [] when the SDK/hardware is absent)
# ──────────────────────────────────────────────────────────────────────────
def list_serial_ports():
    """Available serial ports (COM*/tty*), or ``[]`` if none/unavailable."""
    try:
        from serial.tools import list_ports

        return [p.device for p in list_ports.comports()]
    except Exception:
        logger.debug("serial port enumeration failed", exc_info=True)
        return []


def list_visa_resources():
    """Available VISA resource strings, or ``[]`` if pyvisa/hardware absent."""
    try:
        import pyvisa

        return list(pyvisa.ResourceManager().list_resources())
    except Exception:
        logger.debug("VISA enumeration failed", exc_info=True)
        return []


def list_nidaq_lines():
    """Available NI-DAQ analog-output channels, or ``[]`` if absent."""
    try:
        import nidaqmx  # noqa: F401
        from nidaqmx.system import System

        lines = []
        for dev in System.local().devices:
            lines.extend(ch.name for ch in dev.ao_physical_chans)
        return lines
    except Exception:
        logger.debug("NI-DAQ enumeration failed", exc_info=True)
        return []


def enumerate_choices(choices_from):
    """Dispatch a :class:`Field.choices_from` tag to its enumerator."""
    return {
        "serial": list_serial_ports,
        "visa": list_visa_resources,
        "nidaq": list_nidaq_lines,
    }.get(choices_from, list)()


def coerce_value(raw, typ):
    """Convert a raw string to a :class:`Field` type (shared by CLI + GUI).

    An empty string is returned unchanged; a ``dict`` field is parsed as YAML.
    Raises ``ValueError``/``yaml`` errors on malformed input so callers can
    re-prompt.
    """
    if isinstance(raw, str) and raw == "":
        return raw
    if typ == "int":
        return int(raw)
    if typ == "float":
        return float(raw)
    if typ == "bool":
        return str(raw).lower() in ("1", "true", "yes", "y")
    if typ == "dict":
        import yaml

        return yaml.safe_load(raw) or {} if isinstance(raw, str) else raw
    return raw


# ──────────────────────────────────────────────────────────────────────────
# Build + persist a microscope config entry
# ──────────────────────────────────────────────────────────────────────────
def _section(category, spec):
    """Turn ``{'class': name, 'init_kwargs': {...}}`` into a config block."""
    return {
        "classpath": classpath_for(category, spec["class"]),
        "init_kwargs": dict(spec.get("init_kwargs", {})),
    }


def build_microscope_entry(answers):
    """Assemble a ``configs.yaml`` microscope block from wizard answers.

    ``answers`` keys: ``database`` (server URL or .xlsx path), ``wavelength``,
    ``laser_power`` (ints for the index), ``powermeter`` / ``attenuation`` /
    ``analysis`` (each ``{'class': ..., 'init_kwargs': {...}}``), and optional
    ``lasers`` ({wavelength: spec}) and ``beampath`` ({id: spec}). The driver
    class names are resolved to classpaths via :data:`DRIVERS`.
    """
    name = answers["name"]
    entry = {
        "database": answers.get("database", "./power_database.xlsx"),
        "index": {
            "name": name,
            "wavelength [nm]": answers.get("wavelength", 488),
            "laser_power [mW]": answers.get("laser_power", 100),
        },
        "powermeter": _section("powermeter", answers["powermeter"]),
        "attenuation": _section("attenuation", answers["attenuation"]),
        "analysis": _section("analysis", answers["analysis"]),
    }
    if answers.get("lasers"):
        entry["lasers"] = {
            wl: _section("laser", spec)
            for wl, spec in answers["lasers"].items()
        }
    if answers.get("beampath"):
        entry["beampath"] = {
            obj: _section("beampath", spec)
            for obj, spec in answers["beampath"].items()
        }
    return entry


def _load_yaml(path):
    import yaml

    if path and os.path.isfile(path):
        with open(path, "r") as f:
            return yaml.full_load(f) or {}
    return {}


def existing_entry_names(path):
    """Return the top-level keys already in a YAML config/protocol file.

    ``[]`` when the file does not exist. Lets the wizard show what a shared
    ``configs.yaml`` / ``protocols.yaml`` already holds and warn before a
    same-named entry would overwrite another rig's.
    """
    data = _load_yaml(path)
    return sorted(data) if isinstance(data, dict) else []


def _dump_yaml(data, path):
    """Dump ``data`` to ``path``, backing an existing file up to ``.bak``.

    Same backup convention as :func:`monet.set_config_analysis`.
    """
    import yaml

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.isfile(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except Exception:
            logger.debug("could not back up %s", path, exc_info=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def write_microscope_config(name, entry, configs_path):
    """Insert/replace ``configs[name] = entry`` in ``configs_path`` (backed up).

    Returns the path written.
    """
    configs = _load_yaml(configs_path)
    configs[name] = entry
    _dump_yaml(configs, configs_path)
    return configs_path


def write_protocol_entry(name, protocol, protocols_path):
    """Insert/replace ``protocols[name] = protocol`` (backed up)."""
    protocols = _load_yaml(protocols_path)
    protocols[name] = protocol
    _dump_yaml(protocols, protocols_path)
    return protocols_path


# ──────────────────────────────────────────────────────────────────────────
# Upgrade a single-computer lab (local database files) to a server
# ──────────────────────────────────────────────────────────────────────────
def is_server_database(database):
    """True if a config's ``database`` is a server URL (vs a local file).

    A type-guarded wrapper over :func:`monet.io._is_server_url` (which assumes a
    str); the wizard passes raw config values that may be ``None``/non-str.
    """
    from monet.io import _is_server_url

    return isinstance(database, str) and _is_server_url(database)


def local_db_microscopes():
    """Loaded microscopes whose ``database`` is a local file, not a server URL.

    Empty unless an external ``configs.yaml`` is loaded (the built-in test
    configs are ignored). This is what the wizard offers to migrate to a server
    when a single-computer lab grows. Returns ``{name: config_dict}``.
    """
    if not (getattr(monet, "CONFIGS_PATH", "") or ""):
        return {}
    out = {}
    for name, cfg in (getattr(monet, "CONFIGS", {}) or {}).items():
        if isinstance(cfg, dict):
            db = cfg.get("database")
            if isinstance(db, str) and db and not is_server_database(db):
                out[name] = cfg
    return out


def resolve_local_db_path(database, configs_path=None):
    """Resolve a config ``database`` file reference to an existing path.

    Tries it as given (absolute), then relative to the configs file's directory,
    then relative to the current working directory (monet opens the DB relative
    to the CWD at runtime). Returns the first path that exists, else ``None``.
    """
    if not database or is_server_database(database):
        return None
    candidates = []
    if os.path.isabs(database):
        candidates.append(database)
    else:
        if configs_path:
            candidates.append(
                os.path.join(
                    os.path.dirname(os.path.abspath(configs_path)), database
                )
            )
        candidates.append(os.path.abspath(database))
    for path in candidates:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            return path
    return None


def migrate_local_db_to_server(xlsx_path, server_url):
    """Upload a local Excel calibration DB to a server (preserving history).

    Thin wrapper over :func:`monet.migrate.migrate_excel_to_server` so the CLI
    and GUI import one module. Returns ``(inserted, skipped)``.
    """
    from monet.migrate import migrate_excel_to_server

    return migrate_excel_to_server(xlsx_path, server_url)


def repoint_microscope_to_server(name, server_url, configs_path):
    """Set an existing microscope's ``database`` to ``server_url`` and persist.

    Loads the entry from the in-memory config, swaps only its ``database``, and
    rewrites ``configs_path`` (backed up, other entries preserved). Returns the
    path written, or ``None`` if the microscope is unknown.
    """
    cfg = dict((getattr(monet, "CONFIGS", {}) or {}).get(name, {}))
    if not cfg:
        return None
    cfg["database"] = server_url
    return write_microscope_config(name, cfg, configs_path)


def scaffold_central_folder(folder):
    """Copy the shipped example configs/protocols into an empty ``folder``.

    Only copies a template when the destination does not already exist (never
    clobbers a populated central folder). Returns
    ``{'configs': path, 'protocols': path}`` of the resulting files.

    Raises :class:`FileNotFoundError` when a template source is missing (e.g. a
    packaged install without ``docs/``) rather than silently returning paths to
    files that were never created — the caller can then fall back to plain,
    to-be-authored paths and tell the user.
    """
    os.makedirs(folder, exist_ok=True)
    out = {}
    for key, fname in (
        ("configs", "configs.yaml"),
        ("protocols", "protocols.yaml"),
    ):
        dest = os.path.join(folder, fname)
        src = os.path.join(examples_dir(), fname)
        if not os.path.exists(dest):
            if not os.path.isfile(src):
                raise FileNotFoundError(
                    "example template not found: {} (this monet install has no "
                    "docs/examples; create the config files by hand "
                    "instead)".format(src)
                )
            shutil.copyfile(src, dest)
        out[key] = dest
    return out


# ──────────────────────────────────────────────────────────────────────────
# Micro-Manager config scraping — preset names for protocol beam-path authoring
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class MMConfig:
    """What the wizard needs from a Micro-Manager ``.cfg`` (parsed statically).

    ``groups`` maps each config-group name to its ordered, de-duplicated preset
    names — the exact strings a protocol's ``beampath`` must reference (mismatch
    = a runtime ``Position '…' not available``). ``core_shutter`` is the MM Core
    shutter device (confirms ``NikonShutter`` will work); ``serial_ports`` are
    the COM ports MM assigns (hints only — monet often drives lasers on its own
    ports).
    """

    path: str
    groups: dict
    core_shutter: Optional[str]
    serial_ports: list


def parse_mm_config(path):
    """Parse a Micro-Manager ``.cfg`` into an :class:`MMConfig`.

    The ``.cfg`` is a flat comma-separated text file **without quoting** — that
    is Micro-Manager's own format, and MM forbids commas in group / preset /
    property / value fields (its own loader is a plain comma split too), so the
    naive ``split(",")`` here is correct, not a CSV shortcut to "fix". Needs
    neither Micro-Manager running nor ``pycromanager``; reads the file directly.
    Unknown/irrelevant lines are ignored. Raises the usual ``OSError`` if the
    file cannot be read.
    """
    groups = {}
    core_shutter = None
    ports = []
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            key = parts[0]
            if key == "ConfigGroup" and len(parts) >= 2:
                presets = groups.setdefault(parts[1], [])
                # parts[2] is the preset; a group may be declared with no preset
                # (len == 2), or span many lines (one per device/property).
                if len(parts) >= 3 and parts[2] and parts[2] not in presets:
                    presets.append(parts[2])
            elif key == "Property" and len(parts) >= 4:
                if parts[1] == "Core" and parts[2] == "Shutter":
                    core_shutter = parts[3] or None
                elif parts[2] == "Port" and parts[3] and parts[3] not in ports:
                    ports.append(parts[3])
    return MMConfig(
        path=path,
        groups=groups,
        core_shutter=core_shutter,
        serial_ports=ports,
    )


# ──────────────────────────────────────────────────────────────────────────
# Build a protocols.yaml entry (multi-laser calibration sweep)
# ──────────────────────────────────────────────────────────────────────────
# Beam-path drivers whose protocol value is a boolean (open/close) rather than a
# Micro-Manager preset string. Shared by the CLI and GUI protocol authoring.
SHUTTER_BEAMPATH_CLASSES = {"TestShutter", "NikonShutter"}


def groups_with_presets(cfg):
    """MM config-group names that actually have presets (skip empty groups).

    Shared by the CLI and GUI so the "which group drives this object" choice
    never offers a group whose preset list is empty.
    """
    if cfg is None:
        return []
    return [g for g, presets in cfg.groups.items() if presets]


def is_shutter_beampath(spec):
    """True if a beam-path object's protocol value is a bool (a shutter)."""
    return spec.get("class") in SHUTTER_BEAMPATH_CLASSES


def assemble_protocol_beampath(
    bp_objs, laser_sequence, preset_for, shutter_open_for=None
):
    """Build a protocol ``beampath`` dict (per laser + ``end``) from callbacks.

    One place owns the structure — iterate each step (every wavelength, then the
    special ``end`` park/close step) × each beam-path object, routing shutter
    objects to a bool and the rest to a Micro-Manager preset — so the CLI and
    GUI only supply *how* a value is obtained, not the loop:

    * ``preset_for(step, obj)`` → the preset string for a filter/nosepiece
      object at ``step`` (a wavelength or ``"end"``);
    * ``shutter_open_for(step, obj)`` → the bool for a shutter object; defaults
      to open for a laser step and closed at ``end``.
    """

    def _shutter(step, obj):
        if shutter_open_for is not None:
            return shutter_open_for(step, obj)
        return step != "end"

    beampath = {}
    for step in list(laser_sequence) + ["end"]:
        beampath[step] = {
            obj: (
                _shutter(step, obj)
                if is_shutter_beampath(spec)
                else preset_for(step, obj)
            )
            for obj, spec in bp_objs.items()
        }
    return beampath


def parse_number_list(raw):
    """Parse a comma-separated string into a list of ints (or floats).

    Shared by the CLI and GUI power-set-point fields. Raises ``ValueError`` on a
    non-numeric token so callers can re-prompt.
    """
    out = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok:
            continue
        val = float(tok)
        out.append(int(val) if val.is_integer() else val)
    return out


def build_protocol_entry(answers):
    """Assemble a ``protocols.yaml`` sweep from wizard answers.

    ``answers`` keys: ``laser_sequence`` (list of wavelengths), ``laser_powers``
    ({wavelength: [mW, ...]}), and optional ``beampath`` ({wavelength: {obj:
    value}, plus an ``end`` key} — ``value`` is a preset string (from the MM
    ``.cfg``) for filter/nosepiece objects or a bool for shutters). Returns the
    protocol dict in the schema ``calibrate``/the 2D GUI expect.
    """
    entry = {
        "laser_sequence": list(answers["laser_sequence"]),
        "laser_powers": {
            wl: list(powers)
            for wl, powers in answers.get("laser_powers", {}).items()
        },
    }
    if answers.get("beampath"):
        entry["beampath"] = {
            key: dict(mapping) for key, mapping in answers["beampath"].items()
        }
    return entry


# ──────────────────────────────────────────────────────────────────────────
# Connection check for a just-authored config (no power actuation)
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class DeviceResult:
    """Outcome of probing one device in :func:`test_microscope`."""

    device: str
    ok: bool
    detail: str


def _probe(category, label, block, home=False):
    """Construct one device from its config block and run a cheap read."""
    from monet.util import load_class

    try:
        settings = block.get("settings")
        obj = load_class(block["classpath"], block["init_kwargs"], settings)
    except Exception as exc:
        return DeviceResult(label, False, "could not connect: {}".format(exc))
    detail = "connected"
    try:
        if category == "powermeter":
            detail = "reads {:.4g}".format(float(obj.read()))
        elif category == "attenuation":
            if home:
                obj.home()
            detail = "position {}".format(obj.curr_pos())
        elif category == "laser":
            detail = "enabled={}".format(bool(obj.enabled))
    except Exception as exc:
        # Connected (constructed) but the probe read failed — report, not fatal.
        return DeviceResult(
            label, True, "connected; read failed: {}".format(exc)
        )
    finally:
        _release(obj)
    return DeviceResult(label, True, detail)


def _release(obj):
    """Best-effort release of a hardware handle after probing."""
    for method in ("disconnect", "close", "shutdown"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                logger.debug("release via %s failed", method, exc_info=True)
            return
    try:
        from monet.util import release_hardware

        release_hardware(obj)
    except Exception:
        logger.debug("release_hardware failed", exc_info=True)


def test_microscope(entry, home=False):
    """Probe every device in a built config entry without setting any power.

    Returns a list of :class:`DeviceResult`. ``home`` opts into homing the
    attenuator (a moving-axis action) — off by default so the check never
    swings a half-wave plate unexpectedly.
    """
    results = []
    for category in ("powermeter", "attenuation"):
        if category in entry:
            results.append(
                _probe(category, category, entry[category], home=home)
            )
    for wl, block in (entry.get("lasers") or {}).items():
        results.append(_probe("laser", "laser {}".format(wl), block))
    return results


# ──────────────────────────────────────────────────────────────────────────
# Server-side orchestration
# ──────────────────────────────────────────────────────────────────────────
def guided_server_instructions(host="<linux-server>", port=8000, rig=None):
    """Return copy-paste commands to install the server + mint a rig token.

    This is the manual fallback when SSH automation is unavailable or declined;
    the GUI "Show manual steps" button renders the same text.
    """
    rig = rig or "this-rig"
    script_url = SETUP_SCRIPT_URL.format(ref=DEFAULT_GIT_REF)
    return (
        "# On the Linux server ({host}), as root:\n"
        "curl -fsSL {url} -o setup-server.sh\n"
        "sudo MONET_PORT={port} bash setup-server.sh\n"
        "\n"
        "# Mint a write token for this acquisition machine:\n"
        "sudo -u monet monet token add --scope write --label {rig} "
        "--env-file {env}\n"
        "sudo systemctl restart monet\n"
        "\n"
        "# Then come back here with the server URL and the printed token:\n"
        "#   URL:   http://{host}:{port}\n"
        "#   token: the PAINT_MONET_TOKEN=... value it printed\n"
    ).format(
        host=host, port=port, rig=rig, env=SERVER_ENV_FILE, url=script_url
    )


def parse_token_line(stdout):
    """Extract the token value from ``monet token add`` output.

    ``monet token add`` prints a ``PAINT_MONET_TOKEN=<value>`` line (see
    :func:`monet.tokens._print_new`). Returns the value, or ``None`` if no such
    line is present.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(ENV_CLIENT_TOKEN + "="):
            value = stripped.split("=", 1)[1].strip()
            if value:
                return value
    return None


def remote_install_server(
    host,
    user,
    password=None,
    key_filename=None,
    port=8000,
    ssh_port=22,
    rig=None,
    git_ref=DEFAULT_GIT_REF,
    bind_host="0.0.0.0",
    on_output=None,
):
    """Install/upgrade the monet server on ``host`` over SSH and mint a token.

    Runs ``deploy/setup-server.sh`` (idempotent) then ``monet token add`` for
    this rig, parsing the printed token. Requires the optional ``paramiko``
    dependency (``pip install 'monet[setup]'``) and that ``user`` can ``sudo``.
    Returns ``{'url': ..., 'token': ...}``. Raises on any failure so the caller
    can fall back to :func:`guided_server_instructions`.

    ``on_output`` (optional) is called with each command's combined output for
    live progress display.
    """
    try:
        import paramiko
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "automated SSH install needs paramiko: pip install 'monet[setup]' "
            "— or use the guided manual steps instead."
        ) from exc

    rig = rig or "acquisition-rig"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=ssh_port,
        username=user,
        password=password,
        key_filename=key_filename,
    )

    def run(cmd):
        # Combine stderr into stdout and drain the channel incrementally: a
        # read-stdout-fully-then-stderr sequence deadlocks once either pipe
        # fills, and setup-server.sh can be chatty.
        if on_output:
            on_output("$ {}\n".format(cmd))
        chan = client.get_transport().open_session()
        chan.set_combine_stderr(True)
        chan.exec_command(cmd)
        chunks = []
        while True:
            data = chan.recv(4096)
            if not data:
                break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            if on_output:
                on_output(text)
        code = chan.recv_exit_status()
        chan.close()
        out = "".join(chunks)
        if code != 0:
            raise RuntimeError(
                "remote command failed ({}): {}\n{}".format(code, cmd, out)
            )
        return out

    try:
        install = (
            "curl -fsSL {url} -o /tmp/setup-server.sh && "
            "sudo GIT_REF={ref} MONET_HOST={bind} MONET_PORT={port} "
            "bash /tmp/setup-server.sh"
        ).format(
            url=SETUP_SCRIPT_URL.format(ref=git_ref),
            ref=git_ref,
            bind=bind_host,
            port=port,
        )
        run(install)
        token_out = run(
            "sudo -u monet monet token add --scope write --label {rig} "
            "--env-file {env} && sudo systemctl restart monet".format(
                rig=rig, env=SERVER_ENV_FILE
            )
        )
    finally:
        client.close()

    token = parse_token_line(token_out)
    if not token:
        raise RuntimeError(
            "server installed but could not read the minted token from "
            "'monet token add' output; mint one manually (see the guided "
            "steps) and paste it."
        )
    return {"url": "http://{}:{}".format(host, port), "token": token}
