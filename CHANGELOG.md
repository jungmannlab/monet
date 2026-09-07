# Changelog

All notable changes to **monet** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/). The
version is derived from git tags via setuptools-scm, so cutting a release means:
move the `[Unreleased]` notes into a new `[x.y.z]` section dated today, then
`git tag vx.y.z && git push --tags`.

## [Unreleased]

### Security
- `serve` now binds `127.0.0.1` by default (was `0.0.0.0`). The serve API
  actuates laser hardware, so it must not be network-exposed without
  authentication; binding a non-loopback host will be gated on the shared
  bearer-token auth helper (A9 / ADR-001, WP-3b → reused in WP-12a). Pass
  `--host` explicitly to override on a trusted, authenticated deployment.

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
