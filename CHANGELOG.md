# Changelog

All notable changes to **monet** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/). The
version is derived from git tags via setuptools-scm, so cutting a release means:
move the `[Unreleased]` notes into a new `[x.y.z]` section dated today, then
`git tag vx.y.z && git push --tags`.

## [Unreleased]

### Added
- **One-point calibration rescale ("Pin calibration").**
  `IlluminationLaserControl.pin_calibration()` measures the current laser's
  power at the calibration's peak angle and stores a per-(laser, power)
  multiplicative factor, correcting slow laser-power drift between a full
  calibration and use without re-sweeping. A second check point flags a curve
  *shape* change (where a one-point rescale isn't enough). Exposed as a "Pin
  calibration" button in the Set-power tab's normal view. The weekend drift
  study showed the calibrate-vs-use deviation is (bar a small common thermal
  phase shift) amplitude drift, which this recovers to ~0.5–0.7 %FS for every
  laser line when pinned after warm-up. Pins are cleared when a fresh
  calibration is loaded.
- **Reproducibility probe `power_warmup` experiment + `--plan weekend`.**
  `power_warmup` steps the laser output-power setpoint at a fixed angle and
  watches the meter settle (enable + power-change transients). `--plan weekend`
  runs a staggered protocol (per-line enable→warmup→aging, then an all-lines
  soak, with `--enable-watch` / `--max-hours` auto-shutdown and per-line
  `--disable-after-single`), analysed by `analyze_warmup.py` (warm-up / enable
  vs standby / disable-vs-keep) and `analyze_aging.py` (calibration-aging:
  drift vs systematic, and whether a one-point rescale recovers it).
- **Spline attenuation model (`SplineAttenuationCurveAnalyzer`).** A scipy
  smoothing-spline analyzer for attenuation curves the sinusoidal / polynomial
  models don't capture; the smoothing factor rejects measurement noise (good
  for dim lasers) and it round-trips through the calibration DB via its
  knots/coefficients. Selectable as `spline` in `analysis.model_spec`, the
  Calibrate tab's model dropdown, and the probe's model experiments.
- **Reproducibility probe `model_sweep` experiment.** Calibrates with every
  candidate model at several step sizes and scores each on *fresh off-grid test
  angles* (unbiased generalization error — a spline overfits the fit RMS),
  logging `test_rms_pct` with `n_points` / `acquire_time_s` / `fit_time_s` to
  find the best model + step vs calibration cost (`--sweep-models`,
  `--sweep-steps`, `--n-test`; plotted by `plot_results.py`).
- **Reproducibility probe (`docs/diagnostics/reproducibility_probe.py`).** A
  stand-alone rig diagnostic that isolates the sources of calibrate-vs-measure
  irreproducibility — laser stability, meter dark drift, attenuator
  repeatability, backlash, homing impact, run-to-run fit/model variation, and
  end-to-end set-power deviation — each to its own timestamped CSV, with a
  cycle loop for multi-hour unattended runs. The calibration experiment can
  sweep parameter variants (`--cal-steps`, `--cal-models`) to study
  sampling-density/model impact. Results default to a timestamped directory
  under `docs/diagnostics/results/` (committable for later analysis), and
  `plot_results.py` (or `--plot`) renders a PNG per experiment. Laser-dependent
  experiments run across multiple `(laser, power)` operating points via
  `--lasers`/`--laser-powers` or `--full-protocol` (the config's whole
  line/power grid, with per-laser beam-path routing). A `setpower_breakdown`
  experiment decomposes the open-loop set-power deviation into its inverse-step,
  model-vs-reality and raw-vs-sample-plane (transmission-factor) components to
  localize where it arises. See `docs/diagnostics/README.md`.

## [0.5.0] - 2026-09-30

### Fixed
- **Model-switch robustness (code-review follow-ups).** Selecting a laser with
  no calibration compatible with the current model no longer *sticks* the whole
  instrument uncalibrated — the laser setter now re-establishes calibration from
  the database whenever a valid laser is selected. The fixed-laser/attenuator
  power paths raise a clear "recalibrate this laser" error instead of crashing
  on `min()` of an empty analyzer set, and the base `load_calibration` stays
  uncalibrated (with a warning) instead of crashing on a foreign-model row. A
  read-only Verify now restores each laser's prior on/off state instead of
  switching lit lasers off; the transmission offset-bias check compares the
  slope over the same outlier-filtered points (no spurious warning); and a
  host-built `pc` bound via `MonetWidget.set_pc` picks up the microscope name so
  "Set as default" persists.
- **Switching analysis model no longer crashes with `KeyError: 'nan'`.**
  After switching the model on an already-calibrated instrument, selecting a
  laser rebuilt analyzers from the still-loaded (now-incompatible) rows; with
  those rows skipped the power ranges were empty, so `laserpower` became `NaN`
  and later `KeyError'd`. Selecting a laser with no calibration compatible with
  the current model now falls back to uncalibrated (with a warning), and
  "Apply best model" invalidates the old calibration before recalibrating.
- **Switching analysis model no longer crashes on old calibration rows
  (`KeyError: 'p0'`).** A database can hold rows from more than one analysis
  model (e.g. sinusoidal rows written before switching to polynomial). Loading
  an old sinusoidal row into the polynomial analyzer failed on the missing
  `p0` coefficient (worsened by fragile substring key-matching that treated
  `amp`/`phi` as coefficients). `params2coef` now selects coefficient keys
  strictly (`p0,p1,…`/`i0,i1,…`) and raises a clear error on a foreign model;
  `_populate_analyzers` uses the *latest* calibration per laser power and skips
  rows incompatible with the current model (with a warning); the device-history
  plot skips such rows instead of aborting.

### Added
- **Calibrate tab: analysis-model selector.** A dropdown shows the model in use
  (Sinusoidal / Linear / Polynomial deg 3–6) and **annotates the persisted
  default** `(default)`; changing it switches the model for the session and
  invalidates the current calibration (recalibrate to apply). A **"Set as
  default"** button persists the current selection to the config. "Apply best
  model" switches, recalibrates and sets the default in one step.
- **Expert-view toggle.** A toolbar checkbox (default off) hides controls a
  regular user shouldn't need — the Set Power tab's Backlash check, Refresh
  hardware state, and the direct Attenuator / Laser-power controls — and reveals
  them in expert view. `MonetWidget.set_expert_view()` exposes it for embedders.
- **Within-sweep drift check + durable fit-quality log.** After acquiring a
  calibration sweep, `calibrate()` re-reads the highest-SNR point to measure
  source drift over the sweep (`last_drift_pct`; shown in the Calibrate log and
  added to `last_fit_quality`), and appends one row per calibration to
  `fit_quality_log.csv` (in the plot folder, or the local DB's folder) with the
  timestamp, laser, power, model, RMS/max residual, drift and point count. This
  turns a "runs got worse over the day" impression into a monitorable trend and
  separates source drift from model mismatch. Disable the extra read with
  `calibrate(drift_check=False)`.
- **Set Power tab: a readiness hint next to the Measure button.** A chip
  (and button tooltip) now says whether light is expected to reach the sensor
  — warning `⚠ laser OFF — will read ≈ 0` when the selected laser is off, or
  `⚠ no beam-path preset` when the path won't be routed to the meter, and
  `✓ light expected` otherwise. This explains the common "why did Measure
  return 0?" confusion. The hint now also checks the actual hardware state
  (last-read beam-path positions): it warns `⚠ filter cube not set for <λ> nm`
  when the filter cube in the path doesn't match the selected laser, and
  `⚠ objective in path — need meter in sample position` when the objective
  turret is in the path while the BFP powermeter position is selected (light
  then goes to the sample plane). The shutter is not called out separately
  since autoshutter opens it with the laser.

- **Calibration fit-quality diagnostic, surfaced in the GUI.** After fitting,
  `calibrate()` records the RMS and maximum *relative* residual of the model
  against the calibration data (`last_fit_quality`; per-curve `fit_qualities`
  for a protocol run) and logs it, warning when the RMS exceeds 5%. The
  Calibrate tab now prints this line in its log when a calibration finishes
  (worst curve for a multi-laser run). A large residual means the model
  doesn't describe the attenuator (use more points / a different model); a
  small residual with a large live deviation instead points at laser
  drift/warm-up between calibration and use.
- **Calibration verification (Verify button, Calibrate tab).** For each
  calibrated laser *and* laser-power level, `verify_calibration()` enables the
  laser and routes the beam to the meter (as a calibration does), re-measures a
  few attenuator angles, and compares them to the fitted model in the meter's
  own units — reporting per-point and RMS/max deviation, then switching the
  lasers off again. Unlike the fit residual this is a *fresh* cross-check, so it
  catches drift and laser-power-setting effects (e.g. a deviation that grows
  only at certain laser powers). Each point records its laser and laser power.
- **Model comparison across all curves (Compare models button).**
  `compare_models_multi` fits the sinusoidal and polynomial (deg 3–6) models to
  *every* calibration curve (all wavelengths / powers) and ranks them by the
  residual pooled across all of them — so the model is chosen from the whole
  calibration, not one curve. (`compare_models` remains for a single curve.)
- **Verify against every candidate model.** `verify_calibration` now also
  evaluates each fresh measurement against the sinusoidal and polynomial models
  (`model_summary`: per-model *fit* RMS vs *verify* RMS). This separates model
  accuracy from repeatability: if a better-fitting model verifies better it's
  the model; if all models verify similarly it's drift/repeatability — the
  Verify log states which.
- **Apply best model + recalibrate (button, Calibrate tab).** Switches the
  microscope's analysis model to the best candidate (by fresh-verify residual
  if available, else fit residual — via `analysis.model_spec`) and immediately
  recalibrates. The choice is **persisted to the config file**
  (`monet.set_config_analysis`, which backs up the previous file to
  `<path>.bak`) so it survives a restart; the model is a per-microscope
  setting, so it applies to all of that microscope's lasers.
- **Attenuator backlash check (Set Power tab).** A "Backlash check" button
  re-approaches the current attenuator angle from below and from above, reading
  the power each time; a large power spread points at rotation-mount hysteresis
  — the prime suspect for a calibrate-vs-measure deviation when both are done in
  the same plane (where the objective transmission factor cancels). Backed by
  `IlluminationControl.attenuator_hysteresis_probe()`, which restores the angle
  and clamps both approaches to the calibrated range.
- **Transmission-factor offset-bias diagnostic.** The objective transmission
  factor is stored as a mean of pointwise `P_sample/P_bfp` ratios, which biases
  by tens of percent when one plane has a large additive offset (stray light /
  un-zeroed meter). When computing the factor, monet now also derives the
  offset-immune slope of `P_sample` vs `P_bfp` and logs a warning to
  `monet.log` if the two disagree by >5%, flagging a possibly-biased factor.
  Diagnostic only — the stored factor is unchanged.

### Changed
- **Power meters are now put into power auto-range at open (default on).**
  monet never configured the meter range — and on the TLPM path `open()`
  resets the device, wiping any range set in Thorlabs' Optical Power Monitor
  software — so a fixed, too-low range could silently saturate during a
  calibration and produce the over-range readings behind the fit failure
  below. `ThorlabsPowerMeter` and `ThorlabsTLPMPowerMeter` now enable
  auto-range on connect; set `power_autorange: false` in the powermeter config
  to pin the device's own range instead. Enabling is fail-soft (a driver/API
  mismatch logs a warning and still connects).

### Fixed
- **Calibration aborted with a cryptic "model function generated NaN values"
  error (or silently produced a garbage fit).** A saturated/over-range power
  meter reports the SCPI/IEEE-488.2 sentinel `9.9e37` W, which monet converted
  to ~`1e41` mW and fed straight into the curve fit; because that value is
  *finite* it slipped past ordinary checks and either overflowed the optimizer
  (`sin(inf) → NaN`, lmfit aborts) or converged to a nonsensical model. The
  real-meter read paths (`ThorlabsPowerMeter`, `ThorlabsTLPMPowerMeter`) now
  normalize over-range/non-finite samples to `NaN`, and `calibrate()` guards the
  acquired data before fitting: by default it drops the non-finite point(s) and
  fits the remaining curve, logging the dropped control value(s) and the full
  arrays to `monet.log`. Pass `drop_nonfinite=False` to raise instead; either
  way, if too few finite points remain to fit, it raises a clear error — so a
  bad reading never reaches lmfit or silently produces a garbage calibration.

## [0.4.4] - 2026-09-30

### Fixed
- **`deploy/setup-server.sh` re-run failed with git "dubious ownership".** The
  script's `git`/`pip` steps ran as root against the `monet`-owned
  `/opt/monet/src` checkout, so on any re-run (e.g. to upgrade `GIT_REF`) git
  refused with `fatal: detected dubious ownership` and the source never
  updated. Those steps now run as the `monet` user (repo/venv owner), with an
  ownership fix-up first — so upgrading a deployed server is just
  `sudo GIT_REF=vX.Y.Z bash deploy/setup-server.sh` again.

### Added
- **Deployment: `monet` on PATH + a layout/token guide.** The setup script now
  symlinks `/usr/local/bin/monet → /opt/monet/.venv/bin/monet`, so
  `sudo -u monet monet token …` works without activating anything (the CLI was
  only inside the venv). `docs/deployment.md` gains a **"Where everything lives"**
  map (venv / source / `/etc/monet/monet.env` tokens / DB / log / unit), a
  **"Managing tokens on the deployed server"** section, and a note that the
  service does **not** use any conda env — clearing up the "`monet: command not
  found`, which install is which?" confusion.
- **Dashboard shows the running monet version.** `GET /dashboard/` injects
  `monet.__version__` under the title, so an operator can confirm which build is
  deployed (e.g. whether the dashboard-auth from 0.4.3 is actually running) —
  useful when "is the server up to date?" is the question.

## [0.4.3] - 2026-09-29

### Security
- **The web dashboard now requires a token to view and edit.** Its data routes
  (`/dashboard/api/filters`, `/timeseries`, `/transmission_objectives`) are now
  `read`-scoped, and edits already go through the `write`-scoped main API — so a
  browser needs a valid token to see any data and a `write` token to delete.
  The HTML shell (`GET /dashboard/`) stays public so a small **login prompt** can
  load; the page collects the token, keeps it in `localStorage`, sends it as
  `Authorization: Bearer` on every request, re-prompts on 401, shows the signed-in
  `label`/`scope` (via `/auth/whoami`), and greys the delete controls for a
  read-only token. On an auth-disabled loopback server nothing changes (no login).
  Previously the dashboard was unauthenticated and relied on a reverse proxy
  (ADR-001); it is now self-authenticating too.
- **Dashboard: escape DB record fields before rendering (stored-XSS fix).** The
  All-Records / Latest-Calibrations tables interpolated calibration fields
  (device name, date, parameters) into `innerHTML` unescaped, so a record
  written via `POST /calibrations` with a device named e.g.
  `<img src=x onerror=…>` would execute in another viewer's browser — and, now
  that the dashboard stores a bearer token in `localStorage`, could exfiltrate
  it. All dynamic fields are now HTML-escaped (`esc()`); chart labels already
  used `textContent`. A pre-existing sink, hardened here alongside the auth work.

### Added
- **`monet auth test` CLI + `GET /auth/whoami`.** A client-side command to
  verify a rig can authenticate to a monet server and see the `(scope, label)`
  its token maps to server-side — replacing the ad-hoc curl/probe. Reads the
  same `PAINT_MONET_TOKEN` / `PAINT_MONET_AUTH` the DB client uses, hits
  `/health` (reachability) then the new `/auth/whoami` (auth + identity), and
  prints a verdict (exit 0 = accepted or auth-off, 1 = rejected/unreachable).
  Usage: `monet auth test --url http://server:8000` or `monet auth test <Name>`
  (reads the server URL from that microscope's `database`). `GET /auth/whoami`
  is read-scoped and returns the caller's `TokenInfo`; against an older server
  without it, `auth test` falls back to a read-scoped route to still report
  accepted/rejected (label unavailable until the server is upgraded).

## [0.4.2] - 2026-09-29

### Added
- **systemd deployment template, setup script + docs.** `deploy/monet.service`
  (a unit template for running the DB-only calibration server as a managed
  service), `deploy/setup-server.sh` (one-shot dedicated-user install into
  `/opt/monet` that preserves an existing token file + DB), and
  `docs/deployment.md`. The unit sets `WorkingDirectory`/`HOME` to a writable
  service-owned dir (monet opens a relative `monet.log` at import, which fails
  under `ProtectSystem=strict` when CWD is `/`). Docs cover the dedicated service
  user, `EnvironmentFile` tokens, stop/restart, and troubleshooting for
  `217/USER` / `203/EXEC` / the read-only-`/monet.log` crash / the
  `ProtectHome`-vs-`/root` interpreter gotcha.

## [0.4.1] - 2026-09-29

### Fixed
- **`import monet` no longer crashes from an unwritable working directory.**
  `config_logger()` opened `monet.log` (a relative path) at import and raised
  `OSError` if the CWD was read-only — e.g. a systemd service with `CWD=/` under
  `ProtectSystem=strict` (`OSError: Read-only file system: '/monet.log'`). The
  log location is now configurable via `MONET_LOG_FILE` (full path) or
  `MONET_LOG_DIR` (directory), and an unwritable path falls back to stderr with a
  warning instead of taking down the CLI/GUI/server. Default is unchanged
  (`monet.log` in the CWD).

### Changed
- **Auto-home the attenuator at startup.** `IlluminationControl` now homes the
  attenuator once when the control is constructed (new `auto_home=True` flag,
  threaded through `IlluminationLaserControl`), so every surface — GUI, CLI,
  embedded `monet.qt` widget — starts from a known reference without an operator
  clicking "Home". Homing runs in the base `__init__`, before any lasers are
  loaded or enabled, so the half-wave-plate power swing happens with lasers dark;
  it is wrapped warn-and-continue so a home failure cannot block startup. A
  no-op for attenuators without a moving axis (AOTF, NI-DAQ, TestAttenuator).
  Pass `auto_home=False` to opt out. The headless `serve` power API opts out
  (`build_app_for_microscope` passes `auto_home=False`): serve must not actuate
  hardware at startup — it has no operator to intervene and homing blocks on an
  untimed wait, so a faulted mount would hang server startup. Motion happens
  only via authenticated requests. The CLI `calibrate` command drops its now
  redundant explicit `attenuator.home()` (construction already homes once).
- **`beampath.get_pycromgr` cleanup.** Collapsed the two identical
  `if pycore_config is None / else` branches (both just call `Core()`) into a
  single path, removed the commented-out `pymmcore` scaffolding, and corrected
  the docstring — the `pycore_config` argument is accepted for call-site
  compatibility but not used (the Micro-Manager config is loaded in the MM GUI).
  No behaviour change.
- **pycromanager 1.0 migration complete (B8).** monet's Micro-Manager
  integration targets the pycromanager 1.0 API exclusively — `beampath` connects
  via `Core()` and the `util` acquisition-comment / GUI-refresh helpers use
  `Studio()`; no 0.x API (`Bridge`, etc.) remains. The `>=1.0,<2` `[hardware]`
  pin from 0.4.0 is now validated against the acquisition PCs running
  Micro-Manager nightly build **260917**. (numpy 2, `numpy>=2.2.6,<3`, was
  already in place, so no numeric-stack change was needed.)

## [0.4.0] - 2026-09-28

### Changed
- **Release-prep (C40/C41/B8).** Pinned `picasso-registry[auth]` to the released
  tag **`@v0.1.0`** (was a floating git URL; resolves the C39 caveat — not on
  PyPI, so it stays a pinned git ref). Pinned the `[hardware]` `pycromanager` to
  **`>=1.0,<2`** so the extra is numpy-2-compatible under the C41 harmonization
  (the pycromanager-1.0 code migration + acq-PC validation is tracked as B8;
  monet's pycromanager use is lazy/hardware-only).

### Security
- `serve` now binds `127.0.0.1` by default (was `0.0.0.0`). The serve API
  actuates laser hardware, so it must not be network-exposed without
  authentication.
- **Service authentication (WP-12a / A9 / ADR-001).** The serve API now imports
  the shared bearer-token helper from picasso-registry (`picasso_registry.auth`,
  built in WP-3b) rather than reimplementing auth. Every data route is scoped:
  `write` on DB edits (`/calibrations`, `/factors`, `/calibrations/delete`,
  `/database/restart`) and on `POST /power/set`; `read` on the query routes and
  `GET /power`; `/health` stays public. Tokens are read from `PAINT_MONET_TOKENS`
  (a `token:scope:label` map, never committed, never in the DB); a `write` token
  also satisfies `read`.
- **Fail-closed host guard.** `monet serve` refuses to bind a non-loopback host
  unless tokens are configured, and `require_scope` refuses an unauthenticated
  non-loopback request even if the app is served directly. Loopback dev stays
  zero-config.
- **Authenticated DB client.** monet's own HTTP client (`monet.io`, used by
  `calibrate`/`set`/GUI when `database:` is a server URL) now sends
  `Authorization: Bearer <PAINT_MONET_TOKEN>` on every request, so it keeps
  working when the server enforces auth. Use a `write` token (the client reads and
  writes); unset ⇒ no header (auth-off/loopback servers unchanged). Enabling
  server auth and setting `PAINT_MONET_TOKEN` on clients must be rolled out
  together.

### Fixed
- Connecting in the GUI no longer switches a laser on: loading the calibration
  database populated the analyzers via the ``laser`` setter, which auto-enabled
  the current laser. It now populates them without enabling, so no laser starts
  until the operator switches it on (or a calibration/set-power action does so
  explicitly).
- A completed calibration no longer leaves the shared instrument reporting "no
  calibration available": `CalibrationProtocol2D.run_protocol` reloads the
  calibration database at the end (each 1D step toggles `is_calibrated` off),
  and the Set Power tab refreshes its range display when a run finishes. Power
  could not be set from the Set Power tab after calibrating until reconnecting.
- Calibrate tab: previous-run overlay lines had stopped appearing when the
  database stored the wavelength as a float (or in any form differing from the
  protocol's laser key) — history was matched by string and silently dropped.
  It is now matched by numeric wavelength.
- Database tab: three calibration runs done back-to-back merged into one entry —
  run clustering only split on a >60-min time gap. It now also starts a new run
  whenever a (wavelength, power) combination repeats (a 2D run measures each
  exactly once), so consecutive runs are separated regardless of timing.

### Added
- **`monet token` CLI** to manage server auth tokens without hand-editing files:
  `add`/`list`/`revoke`/`rotate` generate a high-entropy token, maintain the
  `PAINT_MONET_TOKENS` map in a `.env` (chmod 600), and print the value once with
  the client line to paste. `list` shows scopes + labels only, never values.
  Tokens stay plaintext at rest (the C18 model). Run it on the server box — there
  is deliberately no token-minting HTTP endpoint (the dashboard stays
  proxy-guarded per ADR-001).
- **Live token reload on SIGHUP** (Unix). `monet serve` installs a SIGHUP handler
  that re-reads the tokens from `.env` and refreshes the running auth config
  (`server.reload_auth`) — so `monet token add/revoke/rotate` applies without a
  restart via `kill -HUP <serve-pid>`. No-op on Windows (restart there).
- **`.env` for per-machine settings (deprecates `env.yaml`).** monet now loads a
  gitignored `.env` from the package root at import (via `python-dotenv`,
  `override=False`). Config/protocol path lists move to `MONET_CONFIG_PATHS` /
  `MONET_PROTOCOL_PATHS` (`os.pathsep`-separated); `env.yaml` is still read as a
  fallback but emits a `DeprecationWarning`. Auth tokens live here too
  (`PAINT_MONET_TOKEN` / `PAINT_MONET_TOKENS`). See `.env.template`.
- **Auth toggle `PAINT_MONET_AUTH`** (`off` | `on` | `auto`, default `auto`) to
  ease onboarding: `auto` = enforce iff tokens are set (backward-compatible);
  `off` = no auth on loopback and the client omits its token; `on` = require
  tokens (`serve` refuses to start without them). Honoured by the client
  (`monet.io`), the server (`create_app`), and the fail-closed host guard.
- **Target-power API (WP-12a).** `monet serve <MicroscopeName>` now exposes a
  power actuator for the recommender/PycroFlow: `POST /power/set` sets a per-laser
  target power (reusing the closed-loop PI setter `run_power_feedback` when a
  meter is attached, else open-loop from the calibration) and returns the measured
  power so target + measured can be logged to the registry; `GET /power` reads
  back the current power. With no microscope name, `serve` runs the DB only and
  the `/power` + `/laser` routes return `503`.
- **Laser enable/disable API (WP-12a).** `POST /laser/set` `{laser, enabled}`
  toggles one laser's emission (`write`); `POST /laser/off` disables **all**
  lasers **and closes any beam-path shutter** — the fail-safe the
  recommender/PycroFlow calls on end-of-run and on the abort/error path (A10/C21);
  `GET /laser` reports per-laser enabled state (`read`). Emission-off is the hard
  guarantee; shutter-close is best-effort defense-in-depth. All best-effort per
  device (a bad driver is logged, not raised, so one can't block the others).
- **Runtime safety interlock (C34).** A hard per-laser max-power ceiling clamps a
  too-high request to the limit before the laser is actuated (fail-safe, enforced
  in code, not advisory). It is enforced in the **control layer**
  (`IlluminationLaserControl.clamp_to_max_power`, applied by the `power` setter,
  `set_power_fixed_*` and `run_power_feedback`), so **every** actuation path — the
  HTTP power API, the GUI and the CLI — is bounded, not just the API route.
  Configure it via a `safety.max_power_mw` map in the microscope config until the
  versioned site descriptor (WP-FLEET) supplies it; a **malformed** ceiling
  refuses to build the instrument (fail-closed) rather than silently disabling the
  limit. `POST /power/set` reports `clamped` and the delivered `target_power_mw`.
- `monet.serviceauth` binds monet to the shared `picasso_registry.auth` helper
  (via the new `picasso-registry[auth]` dependency in the `[server]` extra) and
  pins monet's own `PAINT_MONET_TOKENS` env var so the two services never share a
  token store.
- Set Power tab: setting a power (without measuring) now records it in the
  MicroManager acquisition comment tagged ``[set]``; a subsequent Measure
  supersedes that line with a ``[measured]`` entry for the same laser
  (`util.update_mm_acquisition_comment` gained a ``kind`` argument and now keys
  the comment line on the wavelength alone).
- Database tab: "Delete run" removes every calibration belonging to the
  selected run(s) (`io.delete_calibration_run`).
- The transmission "Pair runs…" dialog now uses checkboxes to select *which*
  sample and BFP runs to use (click a run to view its curves, tick it to use
  it); every ticked sample run is paired with every ticked BFP run. The tick
  state is restored from the stored pairs, so the dialog can be reopened to
  review and change which runs are in use (`io.clear_factor_pairs`; pairs now
  record the sample/BFP dates and times).
- Larger default main-window size (1200×850).
- Calibrate tab: per-wavelength show/hide toggle buttons above the plots, so a
  wavelength with very low powers can be viewed on its own rescaled axes.
- Calibrate tab: the attenuation-curve plot now overlays the same conditions'
  previous calibrations as thin dated lines, regenerated from the stored fit
  parameters (`io.load_calibration_history`) since the raw points are not kept.
- Calibrate tab: the live "amplitude vs. laser power" plot overlays the most
  recent previous runs as thin faded reference lines (per wavelength, matched
  to the current power-meter position), regenerated from fit parameters, so
  drift is visible while a calibration builds up.
- Calibrate tab: single calibrations whose amplitude strays from the (expected
  linear) amplitude-vs-power trend are flagged in red and listed in a new panel
  where they can be ticked and **discarded** or **recalibrated in place**.
  Outlier detection uses a robust Theil–Sen fit + MAD test
  (`io.flag_amplitude_outliers`); `CalibrationProtocol2D.run_protocol` gained a
  `power_filter` for re-measuring individual points.
- Database tab: a transmission-factor plot showing every objective-transmission
  factor by date, wavelength, and laser power (`io.compute_factor_breakdown`),
  so per-input drift and outliers are visible at a glance.
- Database tab: build objective transmission factors by pairing whole
  calibration **runs** — "Pair runs…" lists the sample-plane and BFP runs
  (clustered from the database by power-meter position and time,
  `io.list_calibration_runs`); tick the runs to use and their single
  calibrations are paired automatically by wavelength and power
  (`io.compute_run_pair_factors`), graphing the ticked runs' curves. Pairs
  persist in a local JSON sidecar (`io.save_factor_pair` / `load_factor_pairs`
  / `compute_pair_factor`), drive the transmission plot, and update the factor
  used for BFP→sample power projection.
- Calibrate tab: a free-text **Comment** field (e.g. "laser status orange
  today") saved with every calibration of a run.

### Changed
- **Stack-wide dependency harmonization (decision C41, picasso is the anchor).**
  Pinned the shared numeric/GUI libs to picasso 0.11.3's shared-lib ranges so
  monet resolves to the same numpy-2 stack as PycroFlow / picasso-workflow when
  co-installed: `numpy>=1.23` → `numpy>=2.2.6,<3`, `pandas>=2.3` →
  `pandas>=2.3.3,<3`, `matplotlib>=3.10` → `matplotlib>=3.10.7,<4`,
  `pyyaml>=6.0` → `pyyaml>=6.0.3,<7`, `PyQt6>=6.5` → `PyQt6>=6.10.2,<7`. Full
  test suite green under numpy 2.
- Plot lines are now coloured by the wavelength's approximate visible-spectrum
  colour (`monet.util.wavelength_to_rgb`) instead of an arbitrary palette, with
  a luminance cap so light colours (yellow/green/cyan) stay legible on white.
- Calibrate tab: the per-wavelength show/hide toggles are drawn white with a
  coloured outline (coloured when shown, greyed when hidden) rather than the
  platform's filled/blue checked style.
- Database tab: the transmission plot now draws the **median** factor per
  wavelength connected over time (temporal evolution), with the individual
  per-date/per-power factors as faint points behind it.
- Pairing dialog / runs plot: each run now has a distinct marker as well as
  line style, and the legend uses longer handles, so solid vs. dashed lines of
  the same colour are told apart.
- Database tab: the record list now shows 2D calibration **runs** (grouped by
  power-meter position and time) instead of single-calibration rows; selecting
  one or more runs plots their amplitude-vs-laser-power curves alongside the
  table (`io.list_calibration_runs` now regenerates per-run amplitudes from the
  stored fit parameters when given the analysis config). Runs carry their
  free-text comment.
- Set Power tab: the status label moved above the power-adjustment box (it was
  between that box and the hardware-settings box).
- Calibrate tab: off-linear flagging now uses a simple 2 % relative-deviation
  threshold against the robust line (was a MAD z-score), matching how operators
  reason about it (`io.flag_amplitude_outliers`).
- Calibrate tab: discarding flagged points now only deletes their records and
  keeps them listed; re-measuring is a separate explicit "Re-measure selected"
  action (no automatic re-acquisition).
- Objective transmission factor: the pooled P_sample/P_bfp ratios now have
  robust (MAD) outliers dropped before averaging, so a single failed
  calibration no longer skews the saved factor (`io.mad_outlier_mask`).
- Calibrate tab: the "BFP powermeter" checkbox is now a "Powermeter position"
  dropdown (BFP / sample plane), matching the Set Power tab's selector.
- Database tab: the record list is now pre-filtered to the connected
  microscope on connect (other scopes remain reachable via the web dashboard
  link).
- Build/versioning aligned to the shared DNA-PAINT stack conventions (S0A-2):
  the version is now derived from the git tag via setuptools-scm (written to
  `monet/_version.py`; `monet.__version__` imports it with a fallback) instead
  of a hand-pinned `version` in `pyproject.toml`. `[tool.black]`
  (`target-version = py310`, line-length 79) and `[tool.flake8]`
  (`extend-ignore = E203,E501,W503`, so black owns line length) now live in
  `pyproject.toml`; the standalone `.flake8` was removed. Added the shared
  pre-commit config (pre-commit-hooks + black + flake8 via Flake8-pyproject) and
  a CI workflow running `black --check`, `flake8`, and `pytest`. black, flake8,
  Flake8-pyproject, and pre-commit were added to the `[dev]` extra. No behavior
  change.
