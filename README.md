# Monet

A Python-based laser power calibration and control suite for microscopy systems. Monet automates calibration of laser power through attenuators (rotation mounts, NIDAQ, AOTF) and power meters, storing results in a SQLite database behind a FastAPI server or in a legacy Excel file. Named after the impressionist painter for his mastery of light intensity.

## Features

- Automated power calibration across multiple lasers and power levels
- Sinusoidal, linear, and polynomial curve fitting for attenuation mapping
- Three power-setting modes — combined (laser + attenuator), fixed-laser (adjust attenuator only), fixed-attenuator (adjust laser only)
- **Closed-loop power setting** with a PI controller against a power meter, with configurable tolerance and gains
- Interactive CLI for calibration, adjustment, and power setting
- **PyQt6 GUI** with a Calibrate / Set Power / Database tab set, including a live feedback-convergence plot
- **Embeddable widget API** (`monet.qt`) — the GUI can be dropped into any other Qt application
- Centralized database server for concurrent multi-microscope access
- Hardware abstraction layer with pluggable laser, attenuator, and power-meter drivers
- Migration tooling from legacy Excel databases to SQLite
- MicroManager integration: measured power is written into the acquisition comment when available

## Prerequisites

**Python >= 3.10** with Anaconda (recommended):

```bash
conda create -n monet python=3.10
conda activate monet
```

**Hardware drivers** are loaded *lazily*, only when the corresponding hardware class is first instantiated. You can install Monet and run it (in test/simulation mode, with the headless test hardware, or as an embedded widget) without any of these. Install the ones you actually use:

- Kinesis rotation mount → Thorlabs Kinesis software (`msl-equipment`)
- Thorlabs PM100 power meter → Thorlabs Optical Power Monitor software (`ThorlabsPM100`, `pyvisa`)
- NI DAQ analog-output attenuator → NI-DAQmx driver (`nidaqmx`)
- Cobolt lasers → `pycobolt`
- Toptica iBeam lasers → `microscope`
- Beam path (filter wheels, shutters) → Micro-Manager + `pycromanager`

## Installation

All dependencies are declared in `pyproject.toml`. The base install covers the
CLI and analysis (and runs against the simulated `Test*` hardware); optional
extras add the GUI, the database server, and the real-instrument SDKs.

```bash
# Base install (development mode)
pip install -e .

# Optional extras — combine in one bracket as needed, e.g. ".[gui,server]"
pip install -e ".[gui]"       # PyQt6 GUI (python -m monet gui)
pip install -e ".[server]"    # FastAPI calibration server (python -m monet serve)
pip install -e ".[hardware]"  # real-instrument SDKs (pyvisa, nidaqmx, ...)
pip install -e ".[dev]"       # test / development tooling
pip install -e ".[all]"       # everything above
```

## Quick Start

### Single-microscope setup (Excel database)

Place the power meter head above the objective, connect the power meter, and switch on the laser.

```bash
# Calibrate a microscope
python -m monet calibrate Voyager

# Set power interactively
python -m monet set Voyager

# Or launch the graphical interface
python -m monet gui Voyager
```

Inside the interactive `set` shell:

```
(monet set) laser 561              # select the 561 nm laser
(monet set) mode fixed_attenuator  # keep attenuator fixed, adjust laser power
(monet set) range                  # show the accessible power range
(monet set) power 50               # set 50 mW (open-loop, from calibration)
(monet set) feedback 50            # set 50 mW closed-loop against the powermeter
(monet set) measure                # read the powermeter + calibration deviation
(monet set) status                 # show all lasers + accessible range
(monet set) exit
```

`mode` accepts `combined` (default), `fixed_laser`, or `fixed_attenuator`. `feedback` and the fixed modes require a multi-power calibration (≥ 2 calibrated laser power levels). PI gains and tolerance for `feedback` are tunable via `feedback_config --kp: 0.9 --ki: 0.1 --tol: 1.0 --max_iter: 20`.

### Multi-microscope setup (database server)

Start the centralized database server on one lab machine. `serve` binds
`127.0.0.1` by default; to reach it from other machines you must expose it on the
network **and** configure authentication (see [Authentication](#authentication)
below) — the server will refuse a non-loopback bind without tokens:

```bash
# tokens configured -> may bind the network interface
export PAINT_MONET_TOKENS="s3cr3t-write:write:microscope-mercury,s3cr3t-read:read:dashboards"
python -m monet serve --db-path /shared/calibrations.db --host 0.0.0.0 --port 8000
```

Then configure each microscope's YAML config to point at the server:

```yaml
database: http://server-hostname:8000
```

All `calibrate`, `set`, `adjust`, and `gui` commands work unchanged — `monet` automatically routes through HTTP when the database path is a URL.

### Target-power API (for the recommender)

Passing a microscope name to `serve` additionally exposes a **target-power API**
so an external recommender (PycroFlow) can set a per-laser power and log the
measured value:

```bash
python -m monet serve <MicroscopeName> --host 127.0.0.1 --port 8000
```

- `POST /power/set` — set a per-laser target power (`write` scope). Reuses the
  closed-loop PI setter when a power meter is attached, else sets open-loop from
  the calibration; returns the measured power. The request is **clamped to a hard
  per-laser safety ceiling** before the laser is actuated (see below).
- `GET /power` — read back the current measured/predicted power (`read` scope).

Without a microscope name, `serve` runs the database only and the `/power` routes
return `503`.

**Safety ceiling (C34).** A monet `write` actuates laser hardware, so power-set
is bounded by a hard per-laser maximum. Until the versioned site descriptor
(WP-FLEET) supplies it, configure the ceiling in the microscope YAML:

```yaml
safety:
  max_power_mw:
    488: 120.0
    561: 200.0
    640: 200.0
```

A request above the ceiling is clamped down (the response flags `clamped: true`
and reports the delivered `target_power_mw`); the hardware is never driven above
the ceiling. Enforcement is in code, not advisory.

### Authentication

The serve API (calibration DB **and** the power actuator) uses the shared
bearer-token auth helper from picasso-registry (`picasso_registry.auth`, WP-3b /
[ADR-001](https://github.com/jungmannlab/picasso-registry/blob/main/docs/adr/001-service-authentication.md)),
so both DNA-PAINT services share one audited implementation.

- **Tokens** live in `PAINT_MONET_TOKENS` (never committed, never in the DB) as a
  comma/semicolon/newline-separated `token:scope:label` map. `scope` is `read` or
  `write`; `label` is the attributable holder (e.g. `microscope-mercury`,
  `cluster`). A `write` token also satisfies `read`.
- **Scopes:** `write` on DB edits (`/calibrations`, `/factors`,
  `/calibrations/delete`, `/database/restart`) and on `POST /power/set`; `read` on
  the query routes and `GET /power`. `/health` is public.
- **Fail-closed:** with no tokens configured the service is unauthenticated — but
  it then refuses any non-loopback bind (startup guard) and any non-loopback
  request (request-time net). Loopback dev stays zero-config.
- **TLS:** terminate TLS at a reverse proxy (Caddy/nginx) or run uvicorn with
  `--ssl-keyfile`/`--ssl-certfile`; put the browser **dashboard** behind the same
  proxy with HTTP Basic / lab SSO (the dashboard is not bearer-guarded, per
  ADR-001). Do not expose plain HTTP off-box.

```bash
# clients send the bearer token
curl -H "Authorization: Bearer s3cr3t-write" \
     -X POST http://scope:8000/power/set \
     -d '{"laser": 488, "target_power_mw": 30}'
```

## Usage

| Mode | Command | Description |
|---|---|---|
| Calibrate | `python -m monet calibrate <Name>` | Run power calibration protocol |
| AOTF Calibrate | `python -m monet caliaotf <Name>` | Calibrate AOTF frequency and power |
| Adjust | `python -m monet adjust <Name>` | Interactive laser alignment and adjustment |
| Set | `python -m monet set <Name>` | Set laser power from existing calibration |
| GUI | `python -m monet gui <Name>` | Launch the PyQt6 graphical interface |
| Serve | `python -m monet serve` | Start the database server |
| Migrate | `python -m monet migrate --source <xlsx> --db-path <db>` | Migrate Excel database to SQLite |

### Server options

```bash
# loopback by default; add a microscope name to enable the power API
python -m monet serve <MicroscopeName> --host 127.0.0.1 --port 8000 --db-path calibrations.db
```

Binding a non-loopback `--host` requires `PAINT_MONET_TOKENS`
(see [Authentication](#authentication)); otherwise the server refuses to start.

### Migration from Excel

If you have an existing Excel calibration database, migrate it to SQLite:

```bash
python -m monet migrate --source power_database.xlsx --db-path calibrations.db
```

This preserves all historical calibration dates and times.

## GUI

`python -m monet gui <MicroscopeName>` opens a PyQt6 window with four tabs:

- **Set Power** — closed-loop feedback control with live convergence plot, accessible-range readout, and direct attenuator / laser power controls.
- **Calibrate** — run 1D or 2D calibration protocols with progress and cancel.
- **Database** — browse calibration records and compute objective-transmission factors.
- **Adjust** — direct alignment controls (also reachable from inside Set Power).

### Embedding Monet in another Qt application

The full GUI (or any individual tab) can be embedded in any host PyQt6 application via the `monet.qt` module:

```python
from monet.qt import MonetWidget, SetPowerTab

# Pattern A — drop the whole 4-tab interface into a host window
widget = MonetWidget(show_toolbar=False)        # hide the microscope picker
widget.set_pc(my_calibration_protocol)          # inject an externally-built pc
widget.status_changed.connect(host.statusBar().showMessage)
host_layout.addWidget(widget)

# Pattern B — drop just one tab next to your own widgets
tab = SetPowerTab()
tab.set_pc(my_calibration_protocol)
tab.status.connect(host.statusBar().showMessage)
host_tabs.addTab(tab, 'Laser')
```

`MonetWidget` exposes:

- `set_microscope(name)`, `connect_microscope(name=None)`, `set_pc(pc)`, `shutdown()`
- Signals: `status_changed(str, int)`, `connected(object)`, `connect_error(str)`, `calibration_started()`, `calibration_finished()`
- `tab('set_power' | 'calibrate' | 'database' | 'adjust')` for direct access to an embedded tab
- Construction options: `show_toolbar`, `tabs=(...)`, `initial_microscope`

A runnable end-to-end demo is in [`examples/embed_monet.py`](examples/embed_monet.py).

## API Reference

When running in server mode, monet exposes these HTTP endpoints:

| Endpoint | Method | Scope | Description |
|---|---|---|---|
| `/calibrations` | POST | `write` | Save a new calibration record |
| `/calibrations/query` | POST | `read` | Query calibration records (supports filtering and time modes) |
| `/calibrations/delete` | POST | `write` | Delete calibration records matching a query |
| `/database/restart` | POST | `write` | Backup current database and prune to latest entries |
| `/factors` | POST | `write` | Save/update an objective-transmission factor |
| `/factors/query` | POST | `read` | Query objective-transmission factors |
| `/power/set` | POST | `write` | Set a per-laser target power (clamped to the safety ceiling); returns measured power. Requires `serve <Name>` |
| `/power` | GET | `read` | Read back measured/predicted power. Requires `serve <Name>` |
| `/dashboard` | GET | proxy | Browser dashboard (guard at the reverse proxy, not by bearer token) |
| `/health` | GET | public | Health check |

Scopes are enforced only when `PAINT_MONET_TOKENS` is set; see [Authentication](#authentication).

## Configuration

Microscope configurations are defined in YAML files referenced by `env.yaml`. Each config specifies:

- `database` — file path (`.xlsx`) or server URL (`http://...`)
- `index` — microscope name, wavelength, laser power
- `powermeter` — classpath and init kwargs
- `attenuation` — classpath and init kwargs
- `analysis` — classpath and init kwargs (curve fitting parameters)
- `lasers` — per-wavelength laser definitions (for 2D protocols)
- `beampath` — filter wheel and shutter definitions

See `monet/__init__.py` for example configurations.

### Available hardware drivers

| Kind | Classes |
|---|---|
| Lasers (`monet.laser`) | `TestLaser`, `MPBVFL`, `Toptica`, `Cobolt`, `Cobolt_OEM`, `LaserQuantum` |
| Attenuators (`monet.attenuation`) | `TestAttenuator`, `KinesisAttenuator`, `AAAOTFAttenuator`, `NIdaqmxAOAttenuator` |
| Power meters (`monet.powermeter`) | `TestPowerMeter`, `ThorlabsPowerMeter` |
| Beam-path devices (`monet.beampath`) | `TestShutter`, `NikonShutter`, `NikonFilterWheel`, `NikonNosepiece` |

`Test*` classes are simulation-only — no hardware or SDK required.

## Architecture

```
monet/
├── __init__.py        # Configuration loading, constants
├── __main__.py        # CLI entry point (calibrate, set, adjust, gui, serve, migrate)
├── calibrate.py       # CalibrationProtocol1D / 2D
├── analysis.py        # Curve fitting (sinusoidal, linear, polynomial, point)
├── control.py         # IlluminationControl / IlluminationLaserControl + run_power_feedback
├── io.py              # Database I/O with Excel/HTTP dispatch
├── cache.py           # Local SQLite mirror + offline outbox for HTTP mode
├── server.py          # FastAPI database server
├── dashboard.py       # /dashboard HTML view served by the FastAPI app
├── models.py          # SQLAlchemy models
├── schemas.py         # Pydantic request/response schemas
├── migrate.py         # Excel → SQLite migration
├── laser.py           # Laser drivers (Toptica, MPBVFL, Cobolt, LaserQuantum, Test)
├── attenuation.py     # Attenuator drivers (Kinesis, AAAOTF, NIdaqmxAO, Test)
├── powermeter.py      # Power-meter drivers (Thorlabs, Test)
├── beampath.py        # Beam path control (filter wheels, shutters)
├── aotf_cali.py       # AOTF frequency/power calibration
├── gui.py             # PyQt6 widget + MonetWidget container + MonetMainWindow
├── qt.py              # Public Qt-level API for embedding
├── util.py            # Dynamic class loading + MicroManager comment writer
└── tests/             # pytest suite (uses Test* hardware)
```

## Testing

```bash
# Run all tests
pytest

# Run a specific test file
pytest monet/tests/test_server.py -v

# Run with coverage report
pytest --cov=monet
```

Tests use `unittest.TestCase` with pytest as the runner. Hardware is simulated via `TestPowerMeter`, `TestAttenuator`, and `TestLaser` classes. Server tests use FastAPI's `TestClient` for in-process testing without a running server. The GUI smoke test (`test_gui_widget.py`) auto-skips when PyQt6 is not installed.

## License

BSD 2-Clause. See [LICENSE.md](LICENSE.md) for details.
