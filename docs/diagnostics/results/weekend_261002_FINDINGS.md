# Weekend drift/warm-up study — findings (2026-10-02, Skylab + Mercury)

Staggered `--plan weekend` runs: each line enabled from standby in turn
(`power_warmup` + 2 h single-line `drift_curve`), then an all-lines soak.
Skylab ran the full 53 h (405→488→642→560, 642 disabled-after-single); Mercury
ran 642→560 then a hardware fault cut the soak (~36 h / 722 curves still
usable). Analysed with `analyze_warmup.py` and `analyze_aging.py` (+ the ad-hoc
`weekend_summary.png` / `analyze_488_shape.png`).

## Headline

The reproducibility limiter is **slow laser-power (amplitude) drift over hours**,
and for every line except Skylab-488 it is a **pure amplitude change** that a
**one-point rescale corrects** back to the fresh-calibration floor.

| rig · line | amp drift over soak | stale-cal set-power err (fresh→aged) | after 1-point | shape kept |
|---|---|---|---|---|
| Skylab 560 | −0.5 % | 0.5 → 0.8 %FS | 0.5 %FS | ✓ |
| Skylab 642 | +4.8 % (±10 %) | 0.4 → 2.5 %FS | **0.5 %FS** | ✓ |
| Skylab 405 | +3.8 % | 0.4 → 1.8 %FS | **0.4 %FS** | ✓ |
| **Skylab 488** | **+17 %** | 0.2 → 5.9 %FS | 6.1 %FS (no help) | ✗ |
| Mercury 560 | +13.9 % | 0.3 → 9.0 %FS | **0.6 %FS** | ✓ |
| Mercury 642 | +5.1 % | 1.2 → 1.7 %FS | 1.2 %FS | ✓ |

## Conclusions

1. **The old 3–18 % deviation was stale-calibration drift, not a systematic
   bias.** Fresh set-power error is 0.2–1 %FS everywhere and grows with
   calibration age to 2.5–9 %FS over ~1.5 days.
2. **It is the laser, not the meter or attenuator.** Different lines on the same
   rig/meter drift by different amounts and signs (meter gain would move them
   together), and the drift is pure amplitude with the curve shape preserved (an
   attenuator/homing drift would shift the shape/phase). Kills the homing
   hypothesis.
3. **One-point rescale ("pin") works** — recovers ≥5 of 6 lines to ~0.5 %FS,
   including the worst drifter (Mercury 560, 9.0 %→0.6 %). Implemented as
   `IlluminationLaserControl.pin_calibration()` + a "Pin calibration" button.
4. **The drift wanders (up and down) over hours**, so warm-up alone isn't
   enough — periodic pin or closed-loop feedback is needed for drifty lines.
5. **Skylab 488 is the exception:** +17 % drift *with* a shape change —
   `analyze_488_shape.png` shows the high-power side grows disproportionately
   (amp +15.5 %, phase drift <1°, so not an angle shift). A one-point pin leaves
   ~3–6 %FS; 488 needs a full recal / investigation.

## Warm-up

- The big transient is **turning emission on**: the first-enabled line (405,
  cold rig) warmed hugely and never settled in 45 min, while lines enabled later
  (longer standby) showed progressively smaller transients → most warm-up is
  **bench/standby thermalisation that finishes while the laser sits powered**.
- Settle times (to 1 % of the 1-h steady): Skylab 560 ~6 min, 642 ~29 min;
  Mercury 560/642 ~45 min (slower rig). **~30 min warm-up is a good minimum,
  ≥45 min on Mercury** — but it does **not** remove the multi-hour wander.
- **Power level barely matters:** once the laser is on, stepping to higher
  setpoints settled in seconds–minutes (no big re-warm). So warming up at any
  power and then going to the working power is fine. (Caveat: we always enabled
  at the lowest level, so a cold start directly at high power wasn't isolated.)
- **Disable-vs-keep A/B (642 disabled, 560 kept):** once the rig was warm,
  re-enabling the disabled line showed **no re-warm penalty** (+0.2 %, like the
  kept lines) — lines can be powered down between uses.

## Data quality notes

- Skylab `single_405` recorded ~0 mW (beam path/filter not set for 405 during
  its single phase) — that warm-up slice is lost; 405's soak data is fine.
- A spurious 890 mW spike on 405's first soak cycle (filtered in analysis).
- Mercury's soak was cut by a hardware fault; its lasers hadn't thermally
  settled even at 45 min.

## Practical recommendation

Warm up ≥30 min (≥45 on Mercury) at roughly the working power, then **Pin
calibration** just before measuring (or use closed-loop feedback). For 488-like
shape-changing lines, the pin self-detects (shape-check warning) and a full
recalibration is needed.
