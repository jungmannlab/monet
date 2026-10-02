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
| `model_sweep` | for each operating point and step size: acquire one sweep, fit every candidate model (`--sweep-models`, default sinus / poly 3 / poly 5 / spline), then score each on **fresh off-grid test angles**; logs `fit_rms_pct` (biased), **`test_rms_pct`** (unbiased generalization error), `n_points`, `acquire_time_s`, `fit_time_s` | the **optimum model + step size**: lowest fresh-test error vs calibration cost. Fit RMS is biased (a spline overfits it to ~0), so rank on `test_rms_pct` |

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
- **`model_sweep`** → in `model_sweep.png`, read `test_rms_pct` (fresh-angle
  generalization error) vs step size, one line per model, per laser; the lowest
  curve is the best model, and the knee against the `acquire_time_s` panel is
  the step size worth paying for. Ignore `fit_rms_pct` for ranking — a spline
  drives it to ~0 by overfitting; only the fresh-test error is honest.
- **`calibration` across `--cal-steps` / `--cal-models`** → compare `rms_pct`
  and `dev_pct` grouped by `step` and `model_variant` (the `calibration.png`
  panels): if finer steps or a different model markedly lower both, sampling
  density / model choice is a real contributor; if they don't move, the
  limiting factor is elsewhere (drift / repeatability).
- **`setpower` `dev_pct`** is the bottom line — correlate its sign/size with the
  other series to find which source dominates.
