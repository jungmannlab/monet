# CLAUDE.md — monet

Standing context for Claude Code (claude.ai/code) working in this repo. Read
this first, then the design doc and the cross-repo pointers below. monet is one
repo in the **DNA-PAINT full-automation** stack (siblings: PycroFlow,
picasso-workflow, picasso, picasso-registry, picasso-agent).

## What this repo is

monet is a Python laser-power **calibration and control** suite for microscopy.
It calibrates each laser's power over its attenuator — Thorlabs Kinesis rotation
mount, NI-DAQ analog-out, or AAOptoelectronic AOTF — against a power meter, fits
the attenuation curve (sinusoidal / linear / polynomial), and then sets power
either open-loop from the calibration or closed-loop via a PI controller against
the meter. Calibrations persist in a SQLite database behind a FastAPI server (or
a legacy Excel file). Three surfaces consume the same calibration core: the
`python -m monet` CLI, a PyQt6 GUI, and an embeddable `monet.qt` widget (used by
PycroFlow's illumination tab).

The importable package is `monet/` (not `PycroFlow`, not `picasso`); hardware
drivers are lazy-imported so the package installs and runs against the simulated
`Test*` hardware with no vendor SDKs. See `README.md` for the full module map,
GUI walkthrough, embedding API, and HTTP endpoints.

## Current branch

Dev branch is `develop`; release path is `develop` → `master`. Work each task on its own feature branch and PR per the **Branch map** in `../../planning/DNA-PAINT_Work-Order-Briefs.md`. Don't pin the current branch here — check the tracker's branch-state note or `git`; a pinned branch is what goes stale.

## Commands

```bash
# Install (base = CLI + analysis against Test* hardware). Add extras as needed.
pip install -e .                # core
pip install -e ".[gui]"         # PyQt6 GUI  -> python -m monet gui
pip install -e ".[server]"      # FastAPI DB server -> python -m monet serve
pip install -e ".[hardware]"    # real-instrument SDKs (pyvisa, nidaqmx, ...)
pip install -e ".[dev]"         # test tooling (pytest, pytest-cov, coverage, httpx)
pip install -e ".[all]"         # everything

# Run (console script `monet` and `python -m monet` are equivalent)
python -m monet calibrate <Name>   # run a calibration protocol
python -m monet set <Name>         # interactive power-setting shell
python -m monet gui <Name>         # PyQt6 GUI
python -m monet serve --db-path calibrations.db --host 0.0.0.0 --port 8000
python -m monet migrate --source power_database.xlsx --db-path calibrations.db
# other modes: adjust, caliaotf

# Test (pytest is configured with testpaths=monet/tests and addopts=--cov=monet,
# so plain `pytest` runs the suite with coverage; needs the [dev] extra)
pytest
pytest monet/tests/test_server.py -v      # a single file

# Lint / format (see Conventions). black + flake8 are NOT yet in the [dev]
# extra — install them explicitly until S0A-2 adds them:
pip install black flake8
black --line-length 79 monet
flake8                                     # reads .flake8
```

## Conventions

These are the conventions **aligned across the DNA-PAINT automation repos**
(see the picasso-registry CLAUDE.md for the canonical block). Where monet's
in-tree config does not match the aligned target yet, the current state is noted
— S0A-2 (the conventions pack) brings the config files into line.

- **Style:** Black, line length **79** (Black owns line wrapping). monet has no
  committed `[tool.black]` yet — pass `--line-length 79` explicitly. Aligned
  target: add `[tool.black]` to `pyproject.toml`.
- **Lint:** flake8, config currently in **`.flake8`** (`max-line-length = 79`;
  `monet/TLPM.py` excluded as vendored; `monet/dashboard.py` exempt from E501
  because it embeds an HTML/CSS/JS string). Aligned target: move to
  `pyproject.toml [tool.flake8]` (via Flake8-pyproject) with `extend-ignore =
  E203, E501, W503` so Black owns line length. Run `flake8` and
  `black --check --line-length 79 monet` before committing.
- **Versioning:** monet currently **pins `version` in `pyproject.toml`**
  (`[project] version`, today `0.3.6`); `monet.__version__` reads it back via
  `importlib.metadata`. Bump it in the same PR as a release and tag `vX.Y.Z`.
  Aligned target (S0A-2): migrate to **setuptools-scm** (version from git tags;
  release = `git tag vX.Y.Z && git push --tags`, no hand-edited version string).
- **Changelog on release:** keep a `## [Unreleased]` section and add an entry in
  every PR; at release, promote `[Unreleased]` to a dated, tagged section
  (`## [X.Y.Z] - YYYY-MM-DD`). monet has **no `CHANGELOG.md` yet** — it will live
  at the repo root once added.
- **Packaging:** `pyproject.toml` only (no `setup.py` / `setup.cfg`). All deps
  and extras live there.
- **Tests:** write/extend tests with every change; keep them green. Hardware is
  simulated via `TestLaser` / `TestAttenuator` / `TestPowerMeter`; server tests
  use FastAPI's `TestClient`; the GUI smoke test auto-skips without PyQt6.

## Working defaults (how to behave in a session)

In-session habits that complement the gates: the STOP-GATE and PR gates govern *when* and *where* to build; these govern *how*.

- **Think before coding.** State your assumptions; if the brief is ambiguous or a simpler approach exists, say so and ask — don't pick silently.
- **Minimal, necessary change.** No speculative abstraction, configurability, or error-handling for cases that can't occur. Every changed line should trace to the work order.
- **Surgical diffs.** Match surrounding style; don't refactor or reformat untouched code, and don't delete pre-existing dead code — mention it instead. Remove only the orphans (imports/vars) your own change creates.
- **Goal-driven.** Turn the brief's ACCEPTANCE into a check you can actually run, and verify it before opening the PR (hosted CI lint + unit is the required merge gate).

## Architecture (short)

Config (`monet/__init__.py`, YAML via `env.yaml`) names per-microscope
`powermeter` / `attenuation` / `analysis` / `lasers` classpaths, dynamically
loaded by `monet/util.py`. `calibrate.py` (`CalibrationProtocol1D/2D`) drives the
hardware, `analysis.py` fits the curve, and `control.py`
(`IlluminationControl` / `IlluminationLaserControl` + `run_power_feedback`) sets
power (open-loop or PI closed-loop). `io.py` dispatches persistence to Excel or
HTTP; `cache.py` is a local SQLite mirror/outbox for the HTTP path;
`server.py` + `models.py` + `schemas.py` + `dashboard.py` are the FastAPI
service. Drivers: `laser.py`, `attenuation.py`, `powermeter.py`, `beampath.py`.
Surfaces: `__main__.py` (CLI), `gui.py` + `qt.py` (PyQt6 GUI / embeddable
`monet.qt`). Full map in `README.md`.

**Auth invariant (fail-closed):** the `serve` API *actuates laser hardware*, so an
unauthenticated networked bind is a safety issue, not just data exposure. The
`--host 0.0.0.0` default above is being changed to `127.0.0.1`; never bind a
non-loopback host without the shared auth helper configured (bearer-token +
`read`/`write` scopes, `write` on power-set / calibration-edit). The helper is
built in picasso-registry (WP-3b) and imported here (WP-12a); see that repo's
`docs/adr/001-service-authentication.md` and Open-Decisions **A9**.

## Standing pointers

Paths so later sessions can `@`-reference them. Repo root is
`/workspaces/DNA-PAINT-FullAutomation/repositories/monet`; the shared workspace
root is `/workspaces/DNA-PAINT-FullAutomation`.

**Live (resolve today):** the shared planning docs live in
`../../planning/` (workspace `planning/` folder); start from its document map.
- Document map / reading order: `../../planning/README.md`
- Design doc — recommendation & roadmap (strategy, prioritized initiatives #1–#9,
  work packages WP-1–WP-16, Parts I–X):
  `../../planning/DNA-PAINT_Automation-Recommendation.md`
- **Playbook** — Claude Code implementation playbook (operating model, Step 0
  foundations, style/repo alignment, gated dependency-ordered work orders):
  `../../planning/DNA-PAINT_ClaudeCode-Implementation-Playbook.md`
- **Work-order briefs** — self-contained, paste-ready briefs (S0A-1, S0A-2,
  S0B-1/2, WP-1…WP-16); this task is S0A-1:
  `../../planning/DNA-PAINT_Work-Order-Briefs.md`
- **Progress tracker** — tick-off worksheet + gates for the work orders:
  `../../planning/DNA-PAINT_Implementation-Progress-Tracker.md`
- Dev-environment setup (OrbStack dev-container):
  `../../planning/DNA-PAINT_ClaudeCode-DevEnvironment.md`
- Sibling repo standing context:
  - PycroFlow (experiment orchestration; consumes `monet.qt` for illumination):
    `../PycroFlow/CLAUDE.md`
  - picasso-registry (provenance/metrics DB; **owns the schema/API contract**):
    `../picasso-registry/CLAUDE.md`
  - picasso-agent (agentic layer): `../picasso-agent/CLAUDE.md`
- Sibling repo roots: `../PycroFlow`, `../picasso-workflow`, `../picasso`,
  `../picasso-registry`, `../picasso-agent`

**Forthcoming (planned; not yet in-tree — do not treat as resolvable):**
- Cross-repo contracts (after S0B): the picasso-registry OpenAPI spec + generated
  client and the shared schemas (metric-vector, workflow-YAML,
  `localize_frames` signature, picasso-workflow `ModuleSpec`) — these will be
  owned by picasso-registry; see `../picasso-registry/CLAUDE.md` and work orders
  S0B-1 / S0B-2 in the briefs above.

## Notes for editing

- `.gitignore` keeps `.claude/` and `CLAUDE.local.md` ignored (local settings /
  personal notes) but **tracks this `CLAUDE.md`** — keep it that way.
- Don't hand-edit `monet/TLPM.py` (vendored Thorlabs wrapper) or expect flake8
  to lint it.
