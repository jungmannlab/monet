# Reproducibility probe

`reproducibility_probe.py` is a stand-alone rig diagnostic that isolates the
individual sources of calibrate-vs-measure irreproducibility. Run it unattended
on a microscope for a few hours; each experiment targets **one** error source
and appends to its own timestamped CSV so you can plot the series and attribute
a deviation to a specific cause instead of guessing.

It is **not** imported by the `monet` package — it just uses the same config,
instrument and power meter the calibration uses (`CONFIGS[<name>]`), so it
exercises the real hardware path.

## Experiments

| experiment | what it does | isolates |
|---|---|---|
| `laser_stability` | laser on, fixed attenuator angle, read power over time | laser warm-up / power instability (+ meter noise) |
| `meter_dark` | laser off, read the meter over time | meter dark/zero drift, ambient light (the noise floor) |
| `repeatability` | set the **same** angle repeatedly, always approached from one direction | attenuator positioning repeatability (backlash removed) |
| `hysteresis` | re-approach one angle from below vs above (backlash probe) | rotation-mount backlash |
| `homing` | read at an angle, **home** the mount, return, read again | whether homing shifts the angle→power mapping |
| `calibration` | run a full sweep + fit repeatedly (dry-run, nothing written to the DB) — optionally over **variants** of step size (`--cal-steps`) and/or model (`--cal-models`); record fit params, RMS/max residual, within-sweep drift, point count, and predicted-vs-measured at reference angles | run-to-run fit variation, model adequacy, and **sampling-density (step) impact** |
| `setpower` | set target powers open-loop from the calibration, measure the actual power | end-to-end reproducibility (what you ultimately care about) |
| `setpower_breakdown` | like `setpower`, but also logs the chosen laser-power level/actual, the achieved attenuator angle, the model's predicted power (`commanded`), the raw and sample-plane readings and the transmission `factor` — and the derived `inverse_err_pct` / `model_err_pct` / raw-vs-sample gap | **where** the set-power deviation comes from: the inverse step (wrong angle), model-vs-reality, or a raw-vs-sample-plane (transmission factor) mismatch |
| `drift_curve` | re-acquire each laser's **full** attenuation curve, timestamped, on a loop (`--cycles`/`--cycle-interval`) over hours or a weekend; `--drift-step` sets the sampling | whether the calibrate-vs-use drift is a pure **amplitude** rescale (a one-point recal would fix it) or a **shape/phase** change — analyze with `analyze_drift.py` / `analyze_aging.py` |
| `pin_check` | replicate the GUI pin→set→measure: measure open-loop set-power deviation at several targets **before** pinning, run `pin_calibration()`, then re-measure the **same** targets. Logs deviation as % of target *and* % of full scale, the pinned getter vs the meter (`calipred_vs_meas_pct`), the laser-power **level** used per target, and the pin factor / shape check | **whether — and where — the one-point pin actually reduces set-power error**: trough inflation (low %FS but high %target), a level switch away from the pinned level, or a getter-vs-reality gap |
| `power_warmup` | at a fixed angle, step the laser **output-power** setpoint (`--warmup-powers`) and watch the meter settle after each change (`--warmup-watch`/`--warmup-interval`), logging `t_since_change` | the thermal transient of **enabling emission** (first level) and of **changing laser power** (later levels): settle time + excursion — analyze with `analyze_warmup.py` |
| `model_sweep` | for each operating point and step size: acquire one sweep, fit every candidate model (`--sweep-models`, default sinus / poly 3 / poly 5 / spline), then score each on **fresh off-grid test angles**; logs `fit_rms_pct` (biased), `test_rms_pct` (per-point relative — inflated by the trough), **`test_fullscale_pct`** (RMS error / max power — trough-robust), `n_points`, `acquire_time_s`, `fit_time_s`. `--spline-smoothing` overrides the spline's auto noise-aware smoothing | the **optimum model + step size**: lowest fresh-test error vs calibration cost. Rank on **`test_fullscale_pct`** (fit RMS is biased — a spline overfits it; per-point relative RMS is trough-dominated) |

Every CSV row carries `iso_time`, `elapsed_s` (since start) and `cycle` (which
battery repeat), so all series share a clock and can be cross-correlated — e.g.
subtract the concurrent `laser_stability` trend from `repeatability` to separate
laser drift from the mount.

### Multiple lasers and powers

The laser-dependent experiments (`laser_stability`, `repeatability`,
`hysteresis`, `homing`, `calibration`, `setpower`) run at one or more
**operating points** `(laser, laser_power)`, and tag every row with `laser` /
`laser_power`:

- `--lasers 488,561,640` and `--laser-powers 100,500` cover that grid, or
- `--full-protocol` covers the config protocol's **entire** `(laser, power)`
  grid — i.e. exactly what a full calibration would sweep, repeated for
  reproducibility across all lines and powers.

For each laser the beam path (filter/shutter, and the BFP turret port for
`--powermeter-type bfp`) is set exactly as calibration does, with a
`--switch-time` settle. Without `--lasers`/`--full-protocol` it falls back to
the single `--laser`/`--laser-power` (backwards compatible).

> Note: `--full-protocol` multiplies run time — `laser_stability` runs its full
> `--duration` at *every* operating point. For long stability soaks use a single
> point; reserve `--full-protocol` for `calibration`/`setpower`.

## Running

Dry-run (no rig; simulated `Test*` hardware, just proves the script works):

```bash
python docs/diagnostics/reproducibility_probe.py --test \
    --cycles 1 --duration 5 --interval 1 --reps 5
```

On a rig (build up a long run — the battery repeats `--cycles` times, waiting
`--cycle-interval` seconds between repeats):

```bash
python docs/diagnostics/reproducibility_probe.py MyScope \
    --laser 488 --laser-power 100 --angle 75 \
    --experiments laser_stability,meter_dark,repeatability,hysteresis,homing,calibration,setpower \
    --duration 1800 --interval 10 --reps 30 \
    --ref-angles 50,75,100 --targets 10,30,100 \
    --cycles 8 --cycle-interval 600 \
    --outdir repro_$(date +%Y%m%d_%H%M)
```

Key options: `--angle` working angle (default: analysis-range midpoint),
`--park` approach offset for directional moves, `--settle` wait after each move,
`--averaging` meter reads per point, `--ref-angles`/`--targets` for the
`calibration`/`setpower` experiments (`--target-fracs 0.25,0.5,0.9` picks
set-power targets as fractions of each laser's *accessible* range so they never
clamp out of range), and `--cal-steps` / `--cal-models` to
sweep the calibration over sampling density / model (e.g.
`--cal-steps 2.5,5,10 --cal-models "sinus,poly deg 5"`). The laser is switched
off and the hardware released on exit (including Ctrl-C, which stops after the
current reading).

`--outdir` defaults to a timestamped directory under
`docs/diagnostics/results/` **inside the repo**, so a run can be committed back
for analysis (see `results/README.md`). Pass `--plot` to render PNGs when the
run finishes, or generate them later:

```bash
python docs/diagnostics/plot_results.py docs/diagnostics/results/run_XXXX
```

## Planning laser-power levels

`suggest_levels.py` proposes which laser output-power **levels** to calibrate so
common set-points sit near a curve's gentle top (accurate) rather than on a
high-power level's steep trough (where low set-points are inaccurate). It reads
each laser's current calibrated levels + sample-plane tops from the DB and, given
the laser maxima, proposes a geometric set from `--floor-frac` of the max up to
the max. It does not move hardware.

```bash
python docs/diagnostics/suggest_levels.py Skylab \
    --laser-max 488:50,560:1000,642:1000 --n-levels 4 --floor-frac 0.10
```

The floor (`--floor-frac`, default 0.10 — a laser can't go below ~10 % of its
max) sets the lowest set-point that any level can serve near a gentle top;
below that, use closed-loop feedback. Pair this with the `pin_check` experiment
to see the actual set-power error per target before re-calibrating.

## Weekend plan (staggered warm-up + aging over a whole weekend)

`--plan weekend` runs a single continuous, multi-phase protocol (it must be one
invocation — the probe disables lasers and re-homes on exit, so phases can't be
split across runs). **Precondition:** manually power every laser to **standby**
(power button + interlock key) but do **not** enable emission yet.

- **Phase 1 — staggered single-line.** For each `--lasers` line in turn:
  software-enable it, run `power_warmup` (the enable transient, watched for
  `--enable-watch`, then each power-setpoint change watched for `--warmup-watch`),
  then loop `drift_curve` for `--single-hours`. Because every line was already
  powered at standby, lines enabled *later* sat at standby longer — so comparing
  their enable transients separates **power-on/standby** warm-up from the
  **emission/enable** transient. Lasers listed in `--disable-after-single` are
  disabled after their phase (cooling at standby); lines not listed stay
  continuously enabled.
- **Phase 2 — multi-line soak.** Re-enable all lines. If any line was disabled,
  a `multi_enable` `power_warmup` pass watches every line settle on re-enable —
  the re-enabled lines show a transient, the kept-enabled ones stay flat (the
  direct "does disabling lose the warm-up?" comparison). Then loop `drift_curve`
  until the `--max-hours` total budget is spent, at which point the run
  **auto-shuts down** (disables emission, releases the hardware).

Rows are tagged with a `phase` column (`single_<laser>` / `multi_enable` /
`multi`); `manifest.csv` records each phase's wall-clock span, lasing set,
whether the single line was `disabled_after`, and which lines were `reenabled`.
Laser power levels for `power_warmup` default to each line's configured
`laser_powers` (omit `--warmup-powers`). `--max-hours` is a hard total cap, so
the whole run finishes and powers down on its own.

```bash
python docs/diagnostics/reproducibility_probe.py Skylab --plan weekend \
    --lasers 405,488,560,642 \
    --single-hours 2 --max-hours 53 --cycle-interval 30 \
    --drift-step 2.5 --settle 0.5 --averaging 20 --switch-time 3 \
    --enable-watch 1800 --warmup-watch 300 --warmup-interval 2 \
    --disable-after-single 488 \
    --outdir docs/diagnostics/results/weekend_$(date +%Y%m%d_%H%M)
# then, per run:
python docs/diagnostics/analyze_warmup.py docs/diagnostics/results/weekend_XXXX
python docs/diagnostics/analyze_aging.py  docs/diagnostics/results/weekend_XXXX
```

The staggered single-line phase measures each line's attenuation curve raw
(angle → meter reading) and fits calibrations **offline** in `analyze_aging.py`;
nothing here loads or depends on a previously stored calibration.

## Calibration aging (is the deviation drift or a systematic bias?)

A `drift_curve` run also feeds `analyze_aging.py`, which turns the periodic
full-curve sweeps into a **calibration-aging matrix** without any extra hardware
pass. It fits each cycle's curve and asks: how well would a calibration made at
hour 0 set power at hour *t*, as it ages? For each target it inverts the
reference (oldest) calibration to an angle and reads the *actual* power off the
later measured curve.

```bash
python docs/diagnostics/analyze_aging.py docs/diagnostics/results/run_XXXX \
    --model "poly deg 5" --ref-cycle 0 --target-fracs 0.25,0.5,0.9
```

It separates the two candidate causes of the calibrate-vs-use deviation:

- **Fresh floor** (recalibrate at use time, the matrix diagonal) — if this is
  already high, the deviation is a **systematic model/factor bias**, not aging.
- **Stale cal** (oldest calibration, aged) vs **+ one-point rescale** — if stale
  rises with age and one-point rescale pulls it back near the fresh floor, the
  deviation is **drift** and a quick one-point recal fixes it.
- The bottom row (none / best-scale / affine curve residual) says whether that
  drift is a pure **amplitude** change (rescale works), a **background** shift
  (needs scale+offset), or a **shape/phase** change (rescale can't fix it).

For this to be meaningful the `drift_curve` run should use a **fine
`--drift-step`** (so the inverse is accurate) and ideally start on a **cold
laser left on continuously** (a single `--lasers` line) so the warm-up transient
is captured — see the aging command at the bottom of "Running".

## Interpreting the output

Plot each CSV against `iso_time`/`elapsed_s`. Rough guide:

- **`laser_stability` drifts / trends up over the first minutes** → laser
  warm-up; calibrate on a settled laser, or use closed-loop feedback.
- **`meter_dark` is non-zero or drifts** → meter zero/ambient; zero the meter,
  block stray light. Subtract this floor from the other readings.
- **`repeatability` spread ≫ `laser_stability` spread** → the attenuator doesn't
  return to the same power for the same command (positioning repeatability).
- **`hysteresis` `power_spread_frac` is large** → rotation-mount backlash;
  approach set-points from one direction, or add backlash compensation.
- **`homing` `rel_change_pct` is large** → the home reference shifts the
  angle→power mapping; re-home before calibrating / setting power, or avoid
  re-homing mid-session.
- **`calibration` `rms_pct`/`dev_pct` vary run-to-run while `drift_pct` stays
  small** → fit/model noise, not drift; try a different model (see the Calibrate
  tab's model comparison). If `drift_pct` grows across runs → source drift.
- **`model_sweep`** → in `model_sweep.png`, read `test_fullscale_pct` (fresh
  off-grid error as % of full scale) vs step size, one line per model, per
  laser; the lowest curve is the best model, and the knee against the
  `acquire_time_s` panel is the step size worth paying for. Ignore `fit_rms_pct`
  (a spline overfits it to ~0) and don't rank on `test_rms_pct` (per-point
  relative error is dominated by the low-power trough) — the full-scale metric
  is the honest one.
- **`calibration` across `--cal-steps` / `--cal-models`** → compare `rms_pct`
  and `dev_pct` grouped by `step` and `model_variant` (the `calibration.png`
  panels): if finer steps or a different model markedly lower both, sampling
  density / model choice is a real contributor; if they don't move, the
  limiting factor is elsewhere (drift / repeatability).
- **`setpower` `dev_pct`** is the bottom line — correlate its sign/size with the
  other series to find which source dominates.
