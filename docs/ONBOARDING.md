# Monet — System Onboarding & Setup Guide

End-to-end setup of a monet installation on a new microscope: Python
environment, hardware drivers and their config, Micro-Manager integration
(and making the two configs match), and the central calibration server.

This guide is task-ordered. If you just want to get one microscope running
against an Excel file, do Parts 1–4. Add Micro-Manager (Part 5) and the
server (Part 6) as needed.

> Terminology: a **microscope config** is one named entry in `configs.yaml`.
> A **protocol** (in `protocols.yaml`) describes a multi-laser/multi-power
> calibration sweep. `<Name>` throughout is the config's top-level key
> (e.g. `Voyager`), passed on the command line.

> **Ready-to-edit examples:** a cross-consistent bundle of all four config
> files (`env.yaml`, `configs.yaml`, `protocols.yaml`, and a Micro-Manager
> `microscope.cfg`) lives in [`docs/examples/`](examples/) — see
> [`examples/README.md`](examples/README.md) for how their names line up.
> The snippets throughout this guide are excerpts from that bundle.

---
## Part 0 — Third party software installation
consider installing the following third party software
* Anaconda / Miniconda
* Micromanager
* Laser communication software (currently supported: MPB VFL, Cobolt, Toptica)
* Thorlabs Kinesis
* Thorlabs OpticalParameterMonitor
* VISA: KeySight IOLibrariesSuite

---
## Part 1 — Python environment & install

Python **≥ 3.10** (3.10 recommended). Anaconda/Miniconda makes the vendor SDKs easier
to live with on Windows.

```bash
conda create -n monet python=3.10
conda activate monet

cd <path-to>/monet
pip install -e .                 # core: CLI + analysis + Test* simulation hardware
```

Add extras for the surfaces/hardware you actually use (combine in one
bracket, e.g. `.[gui,server,hardware]`):

| Extra | Adds | Enables |
|---|---|---|
| `.[gui]` | PyQt6 | `python -m monet gui` |
| `.[server]` | FastAPI + uvicorn | `python -m monet serve` |
| `.[hardware]` | pyvisa, nidaqmx, … | real instruments |
| `.[all]` | everything | — |

Hardware SDKs are **lazy-imported** — the package installs and runs against
the simulated `Test*` hardware with none of them present. Install a vendor
SDK only when you actually instantiate that device:

- Kinesis rotation mount → Thorlabs Kinesis software (`msl-equipment`)
- Thorlabs PM100 power meter → Thorlabs Optical Power Monitor + `ThorlabsPM100`, `pyvisa`
- Newer Thorlabs TLPM meter (PM100D2 / TLPM-bound PMxxx) → Optical Power Monitor SDK + vendored `monet/TLPM.py` (see Part 4)
- NI-DAQ analog-out attenuator → NI-DAQmx driver (`nidaqmx`)
- Cobolt lasers → `pycobolt`; Toptica iBeam → `microscope`
- Beam path (filter wheels/shutters) + acquisition-comment writing → Micro-Manager + `pycromanager`

**Smoke test** against simulated hardware (no SDKs, no config files needed):

```bash
python -m monet gui test          # or: python -m monet set test
```

`test` / `test_2D` are built-in configs used automatically when no
`configs.yaml` is found.

---

## Part 2 — The config chain (env.yaml → configs.yaml / protocols.yaml)

At import time `monet/__init__.py` loads `env.yaml` from the **repo root**
(next to `monet/`). `env.yaml` contains only two lists of file paths:

```yaml
# env.yaml  (copy from env_template.yaml and edit)
config_paths:
- Z:/users/myuser/monet_config/configs.yaml
- Z:/alternativefallback/powerbase/configs.yaml
protocol_paths:
- Z:/users/myuser/monet_config/protocols.yaml
- Z:/alternativefallback/powerbase/protocols.yaml
```

- Monet tries each path **in order** and loads the **first one that opens**
  into the global `CONFIGS` (and likewise `PROTOCOLS`). Put a machine-local
  path first and a shared-drive fallback second.
- If **no** `config_paths` file loads, monet falls back to the built-in
  `default` / `test` / `test_2D` configs. (That's what the smoke test uses.)
- `configs.yaml` is a dict keyed by **microscope name**; `<Name>` on the CLI
  selects `CONFIGS[<Name>]`. An unknown name raises a `KeyError` that prints
  the available names.

A ready-to-edit version is [`docs/examples/env.yaml`](examples/env.yaml)
(shipped pointing at the example bundle, so `cp docs/examples/env.yaml
env.yaml` works out of the box).

**Setup steps on a new machine**

1. `cp env_template.yaml env.yaml` (or `cp docs/examples/env.yaml env.yaml`) at
   the repo root, and edit the four paths to point at where you keep
   `configs.yaml` / `protocols.yaml` for this machine.
2. Create `configs.yaml` (Part 3) and, for multi-laser sweeps,
   `protocols.yaml` (Part 5.4).
3. You can override the config/protocol files per-invocation with
   `-c/--configs-file` and `-p/--protocol-file` (calibrate/adjust modes).

> Note: `env.yaml` is resolved relative to the installed `monet` package, so
> run monet from a source checkout (`pip install -e .`) or keep `env.yaml`
> beside the package. Confirm on startup — monet prints
> `Loaded configurations from <path>`.

---

## Part 3 — A microscope config (hardware access)

Each entry in `configs.yaml` fully describes one microscope. Required keys:
`database`, `index`, `powermeter`, `attenuation`, `analysis`. Optional:
`lasers` (for 2D/multi-laser protocols), `beampath` (Micro-Manager devices),
`dest_calibration_plot`. A complete three-microscope example (`sim`,
`Voyager`, `Deepglow`) is in [`docs/examples/configs.yaml`](examples/configs.yaml);
the `Voyager` entry is excerpted below.

```yaml
Voyager:                                   # <Name> on the CLI
  database: ../power_database.xlsx         # .xlsx path OR http://host:8000 (Part 6)

  index:                                   # primary key for every calibration record
    name: Voyager                          # DEVICE_TAG = "name"
    wavelength [nm]: 488                   # LASER_TAG  = "wavelength [nm]"
    laser_power [mW]: 100                  # POWER_TAG  = "laser_power [mW]"

  powermeter:
    classpath: monet.powermeter.ThorlabsPowerMeter
    init_kwargs:
      address: find connection             # auto-detect first Thorlabs VISA meter

  attenuation:
    classpath: monet.attenuation.KinesisAttenuator
    init_kwargs:
      serial: "272348733"                  # Serial number printed on the Kinesis Cube

  analysis:
    classpath: monet.analysis.SinusAttenuationCurveAnalyzer
    init_kwargs: {min: 40, max: 100, step: 5}  # angular range to probe wiht half wave plate

  # OPTIONAL — required for 2D / multi-laser protocols (Part 5.4)
  lasers:
    488: {classpath: monet.laser.Toptica, init_kwargs: {port: COM4}}
    561: {classpath: monet.laser.MPBVFL,  init_kwargs: {port: COM7}}
    640: {classpath: monet.laser.MPBVFL,  init_kwargs: {port: COM8}}

  # OPTIONAL — Micro-Manager beam-path devices (Part 5)
  beampath:
    DC:      {classpath: monet.beampath.NikonFilterWheel, init_kwargs: {SN: n/a}}
    shutter: {classpath: monet.beampath.NikonShutter,     init_kwargs: {SN: n/a}}

  dest_calibration_plot: ./                 # OPTIONAL — where fit plots are written
```

The `index` triple (`name`, `wavelength [nm]`, `laser_power [mW]`) plus
date/time forms the database key for a calibration record — keep `name`
consistent for a microscope across all its entries.

### 3.1 Available drivers and their `init_kwargs`

Every hardware block is `{classpath, init_kwargs}`; `init_kwargs` is passed
verbatim to the driver's `__init__`. `Test*` classes need no SDK or hardware.

**Power meters** (`monet.powermeter`)

Powermeters enable automatic power measurements and calibration.

| Class | Key `init_kwargs` |
|---|---|
| `TestPowerMeter` | `address`, plus sim params `bkg, amp, phi, start, step, noise` |
| `ThorlabsPowerMeter` | `address` (`"find connection"` or a VISA resource) — legacy USBTMC/VISA meters (PM100D) |
| `ThorlabsTLPMPowerMeter` | `address` (opt., default `"find connection"`), `dll_path` (opt.) — newer TLPM-driver meters; see **Part 4** |

**Attenuators** (`monet.attenuation`)

Attenuators modify laser power. Monet currently supports attenuation via AOTF, or half-wave plate rotation with polarizing beam splitter cube.

| Class | Type | Key `init_kwargs` |
|---|---|---|
| `TestAttenuator` | default for testing | `bkg, amp, phi, start, step` |
| `KinesisAttenuator` | Thorlabs-motorized halfwave plate | `serial` (rotation-mount serial) |
| `AAAOTFAttenuator` | AOTF-attenuation (AA) | `port`, `channeldef_loc` (CSV wavelength→channel map) |
| `NIdaqmxAOAttenuator` | AOTF-attenuation (Analog) | `lines` (dict wavelength→AO line, e.g. `{488: "Dev1/ao0"}`) |

**Lasers** (`monet.laser`) — only needed for `lasers:` / 2D protocols

| Class | Key `init_kwargs` |
|---|---|
| `TestLaser` | `{}` |
| `MPBVFL`, `Toptica`, `LaserQuantum` | `COM port` (+ optional serial params) |
| `Cobolt`, `Cobolt_OEM` | `port` or `serialnumber`, `baudrate` (`Cobolt_OEM` = keyless) |

**Analysis / curve fit** (`monet.analysis`)

Power vs attenuation model needs to match the attenuation type. E.g. half wave plate rotation results in a sinusoidal pattern with the rotation angle, while an AOTF results in a polynomial with respect to the input voltage.

| Class | Key `init_kwargs` |
|---|---|
| `SinusAttenuationCurveAnalyzer` | `min, max, step` |
| `LinearAttenuationCurveAnalyzer` | `min, max, step` |
| `PolynomialAttenuationCurveAnalyzer` | `min, max, step, degree` |

**Beam-path devices** (`monet.beampath`) — see Part 5.

### 3.2 Verify hardware access

```bash
python -m monet set Voyager        # opens the interactive shell
(monet set) status                 # lists lasers + accessible range → confirms connections
(monet set) measure                # reads the power meter → confirms the meter
```

If the power meter is the thing that fails, go to Part 4.

---

## Part 4 — Thorlabs power meter: legacy vs. TLPM driver

Two generations of Thorlabs meters need **different driver classes**:

- **Legacy PM100D** on the USBTMC/VISA driver → `ThorlabsPowerMeter`
  (pyvisa-based; the meter shows up as a VISA resource).
- **Newer meters** (PM100D2, or any PMxxx bound to the **TLPM driver** that
  Thorlabs' *Optical Power Monitor* installs) → `ThorlabsTLPMPowerMeter`.
  These **do not appear as VISA resources at all**, so the pyvisa driver can
  never find them. This is the classic "everything works except the
  powermeter" symptom on a fresh machine.

**Switch to the TLPM driver:**

1. Install Thorlabs **Optical Power Monitor** (installs the TLPM driver + DLLs).
2. Point the config at the TLPM class:
   ```yaml
   powermeter:
     classpath: monet.powermeter.ThorlabsTLPMPowerMeter
     init_kwargs:
       address: find connection
       dll_path: C:/Program Files/IVI Foundation/VISA/Win64/Bin   # if TLPM_64.dll not on PATH, installed by Thorlabs Optical Parameter Monitor
   ```
3. Ensure `monet/TLPM.py` is present (it's vendored in this repo). If a bare
   checkout lacks it, copy Thorlabs' wrapper from
   `C:\Program Files\IVI Foundation\VISA\Win64\TLPM\Examples\Python\TLPM.py`
   into the `monet/` package directory.
4. Set `dll_path` to the folder containing `TLPM_64.dll` if it isn't on the
   system PATH (the wrapper loads the DLL by bare name).
5. Make sure *Optical Power Monitor* isn't holding the meter open when monet runs.

Quick detection check on the target machine:

```bash
python -c "import ctypes; from monet.TLPM import TLPM; p=TLPM(); n=ctypes.c_uint32(0); p.findRsrc(ctypes.byref(n)); print('meters found:', n.value)"
```

`meters found: 0` → DLL loaded but no meter (driver/cabling/other app holding it).
An `ImportError`/`OSError` → `TLPM.py` or `TLPM_64.dll` not found (fix steps 3–4).

---

## Part 5 — Micro-Manager integration

Monet talks to Micro-Manager through **pycromanager** for two independent
purposes. Both are optional and no-ops if pycromanager isn't installed.

1. **Beam-path control** — `NikonShutter` / `NikonFilterWheel` /
   `NikonNosepiece` drive Micro-Manager **config groups** via `pycromanager.Core`.
2. **Acquisition annotation** — after a `measure`/`feedback`/set, monet writes
   the power into Micro-Manager's **acquisition comment** and refreshes the MM
   GUI via `pycromanager.Studio` (`monet/util.py`).

### 5.1 Prerequisites & connection

- Install Micro-Manager, then `pip install pycromanager`.
- **Micro-Manager must be running** with the ZMQ/Java gateway enabled
  (in MM: *Tools → Options → "Run server on port 4827"*, the pycromanager
  default). Monet's `Core()` connects to that gateway; if MM isn't up you get
  a clear `TimeoutError` ("Check that Micro-Manager is running and the Java
  gateway is accessible").
- Load your microscope's hardware configuration (`.cfg`) in Micro-Manager as
  usual. Monet does **not** load a `.cfg` itself — it queries whatever MM
  currently has loaded.

A loadable, fully-worked example is [`docs/examples/microscope.cfg`](examples/microscope.cfg)
— built on Micro-Manager's DemoCamera devices so it opens with no real
hardware, with the group / preset / role names already set to match the
example `Voyager` protocol.

### 5.2 Making the two configs match (the important part)

Monet reads Micro-Manager **config groups** and **presets** by name. These
names must line up between your MM hardware config and monet:

- `NikonFilterWheel` looks for a config group named **`Filter turret`**
  (case-insensitive, with fuzzy fallback on the words).
- `NikonNosepiece` looks for a group named **`Nosepiece`**.
- `NikonShutter` uses the MM core shutter (`Core`/`AutoShutter`,
  `setShutterOpen`) — no group needed. Monet sets `AutoShutter=0` and drives
  the shutter explicitly.

The **preset names** monet sends to a group must be presets that exist in that
group in Micro-Manager. Those preset strings come from your **protocol's
`beampath` section** (Part 5.4). Example: if a protocol says
`{DC: "Ti488setting"}`, then the MM `Filter turret` group **must contain a
preset called `Ti488setting`**, or monet raises
`Position 'Ti488setting' not available … Options: [...]`.

Checklist to match them:

1. In Micro-Manager, create config groups named `Filter turret` and (if used)
   `Nosepiece`.
2. Add one preset per optical setting, and **name the presets exactly** what
   your monet protocol references (`Ti488setting`, `Ti561setting`, …).
3. Confirm the shutter device is set as the MM **Core shutter**.
4. Keep the `beampath` object ids in the *microscope config* (`DC`, `shutter`,
   …) consistent with the ids used in the *protocol* `beampath` entries.

> Redundant moves: re-selecting a filter/nosepiece group's *current* preset
> makes the Nikon Ti controller error (`0xe01004b6`), so monet skips a move
> when the group is already at the requested preset.

### 5.3 Acquisition-comment annotation

No config needed beyond a running MM with `pycromanager`. On a measurement,
monet writes/updates a line keyed on wavelength into the acquisition comment,
e.g.:

```
Power 561nm [measured]: 42.500 mW @ att=65.4000 lp=75.0mW [BFP reading 42.800 mW x T_obj=0.9865]
```

A later `[measured]` line supersedes an earlier `[set]` line for the same
laser. If pycromanager isn't installed, this is silently skipped.

### 5.4 Protocol file (`protocols.yaml`) for multi-laser sweeps

Keyed by microscope name; drives `calibrate`/2D runs and the beam path. Full
example: [`docs/examples/protocols.yaml`](examples/protocols.yaml).

```yaml
Voyager:
  laser_sequence: [488, 561, 640]
  laser_powers:
    488: [100, 200, 500, 1000]
    561: [200, 500, 1000, 2000]
    640: [200, 500, 1000, 2000]
  beampath:                                # values must match MM presets (5.2)
    488: {DC: Ti488setting, shutter: true}
    561: {DC: Ti561setting, shutter: true}
    640: {DC: Ti640setting, shutter: true}
    end: {DC: Ti488setting, shutter: false}   # park + close at the end
```

Run it with `python -m monet calibrate Voyager` (uses the protocol from
`protocol_paths`, or `-p <file>` to override). Without a protocol, calibrate
runs a 1D attenuator-only sweep (no laser control).

---

## Part 6 — Central calibration server

For multi-microscope / multi-machine setups, run one FastAPI server backed by
SQLite; every client points its `database` key at the server URL. Clients keep
working offline via a local cache + outbox that replays when the server
returns.

### 6.1 Start the server

On one lab machine (`pip install -e ".[server]"`):

```bash
python -m monet serve --db-path /shared/calibrations.db --host 0.0.0.0 --port 8000
```

Defaults: `--host 0.0.0.0`, `--port 8000`, `--db-path calibrations.db`.
(`serve` sets `MONET_DB_PATH` and launches uvicorn; SQLite runs in WAL mode
for safe concurrent reads.)

Verify:

```bash
curl http://localhost:8000/health          # -> {"status":"ok"}
# browse stored calibrations in a browser:
#   http://<server-host>:8000/dashboard
```

### 6.2 Point clients at the server

In each microscope's config, set `database` to the server URL instead of an
`.xlsx` path — that's the **only** change; monet auto-detects `http(s)://`:

```yaml
Voyager:
  database: http://server-hostname:8000
  # ... rest of the config unchanged ...
```

All `calibrate`, `set`, `adjust`, and `gui` commands then route through HTTP.
If the server is unreachable, writes queue in `~/.monet/cache_<hash>.db` and
reads fall back to the local mirror; the outbox flushes on the next successful
call (with a short cooldown between retries).

### 6.3 Migrate an existing Excel database

```bash
python -m monet migrate --source power_database.xlsx --db-path /shared/calibrations.db
```

Preserves historical calibration dates/times. Point the server's `--db-path`
at the resulting file.

### 6.4 HTTP API (reference)

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/calibrations` | Save a calibration record |
| POST | `/calibrations/query` | Query records (`time_idx`: `latest` / `last date` / `last combinations` / `all` / `[date[,time]]`) |
| POST | `/calibrations/delete` | Delete records (optional filters) |
| POST | `/factors`, `/factors/query` | Objective-transmission factors |
| POST | `/database/restart` | Back up DB and prune to latest entries |
| GET | `/dashboard` | Browser dashboard (Plotly) |
| GET | `/health` | Health check |

**Security note:** the server ships with **no authentication, no CORS, no
TLS**, and `0.0.0.0` binds all interfaces. Keep it on a trusted lab network,
bind `127.0.0.1` for local-only use, or front it with a reverse proxy
(nginx/caddy) for auth/HTTPS.

---

## Part 7 — GUI & embedding (optional)

```bash
python -m monet gui Voyager
```

Four tabs: **Set Power** (closed-loop feedback + live convergence plot),
**Calibrate**, **Database** (browse records, compute objective-transmission
factors), **Adjust**.

The whole widget or a single tab can be embedded in another PyQt6 app (this is
how PycroFlow's illumination tab consumes monet):

```python
from monet.qt import MonetWidget, SetPowerTab
widget = MonetWidget(show_toolbar=False)
widget.set_pc(my_calibration_protocol)
widget.status_changed.connect(host.statusBar().showMessage)
```

See `examples/embed_monet.py` for a runnable demo.

---

## Quick end-to-end checklist (new machine)

1. `conda create -n monet python=3.10 && conda activate monet`
2. `pip install -e ".[gui,hardware]"` (+`server` where the server runs)
3. `python -m monet gui test` — confirm the app runs on simulated hardware
4. `cp env_template.yaml env.yaml`; edit `config_paths` / `protocol_paths`
5. Write `configs.yaml` with your microscope entry (Part 3); pick the right
   power-meter class (Part 4 for TLPM meters)
6. `python -m monet set <Name>` → `status`, `measure` to verify each device
7. Micro-Manager: start it with the pycromanager gateway; create `Filter
   turret` / `Nosepiece` groups with presets named to match your protocol
   (Part 5.2)
8. Write `protocols.yaml`; `python -m monet calibrate <Name>`
9. For multi-microscope: start the server, migrate any Excel DB, set each
   config's `database:` to `http://host:8000` (Part 6)

---

*Cross-references: ready-to-edit config bundle in
[`docs/examples/`](examples/); module map, GUI walkthrough and embedding API in
[`README.md`](../README.md); repo conventions and cross-repo pointers in
[`CLAUDE.md`](../CLAUDE.md).*
