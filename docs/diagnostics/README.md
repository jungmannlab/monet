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
| `calibration` | run a full sweep + fit repeatedly (dry-run, nothing written to the DB); record fit params, RMS/max residual, within-sweep drift, and predicted-vs-measured at reference angles | run-to-run fit variation & model adequacy |
| `setpower` | set target powers open-loop from the calibration, measure the actual power | end-to-end reproducibility (what you ultimately care about) |

Every CSV row carries `iso_time`, `elapsed_s` (since start) and `cycle` (which
battery repeat), so all series share a clock and can be cross-correlated — e.g.
subtract the concurrent `laser_stability` trend from `repeatability` to separate
laser drift from the mount.

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
`calibration`/`setpower` experiments. The laser is switched off and the hardware
released on exit (including Ctrl-C, which stops after the current reading).

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
- **`setpower` `dev_pct`** is the bottom line — correlate its sign/size with the
  other series to find which source dominates.
