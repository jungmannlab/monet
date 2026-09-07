# Example config bundle

A cross-consistent set of configuration files referenced by
[`../ONBOARDING.md`](../ONBOARDING.md). Read them together — names are shared
across files on purpose.

| File | What it is | Onboarding part |
|---|---|---|
| [`env.yaml`](env.yaml) | Top-level discovery file → copy to the repo root as `env.yaml` | Part 2 |
| [`configs.yaml`](configs.yaml) | Three microscopes: `sim`, `Voyager`, `Deepglow` | Part 3 |
| [`protocols.yaml`](protocols.yaml) | Calibration sweeps for `Voyager` and `sim` | Part 5.4 |
| [`microscope.cfg`](microscope.cfg) | Micro-Manager hardware config (loadable DemoCamera template) | Part 5 |

## How the names line up (the contract)

```
protocols.yaml (Voyager)          microscope.cfg                 configs.yaml (Voyager)
  beampath:                         ConfigGroup "Filter turret"    beampath:
    488: {DC: Ti488setting} ─────►    preset  Ti488setting           DC:      NikonFilterWheel
    561: {DC: Ti561setting} ─────►    preset  Ti561setting           shutter: NikonShutter
    640: {DC: Ti640setting} ─────►    preset  Ti640setting
        {shutter: true/false} ───►  Core "Shutter" role  ◄───────────┘
```

Change a preset name in one place and you must change it in all three, or
monet raises `Position 'Ti488setting' not available …`.

## Try it end-to-end (no hardware)

```bash
# from the repo root, with env.yaml pointing at docs/examples/ (as shipped):
cp docs/examples/env.yaml env.yaml
python -m monet gui sim          # simulation config — no SDKs, no MM
python -m monet calibrate sim    # runs the sim sweep from protocols.yaml
```

To also exercise the Micro-Manager path in simulation, load
`microscope.cfg` in Micro-Manager, enable the pycromanager gateway, and point
a config's `beampath` at the `Nikon*` classes.
