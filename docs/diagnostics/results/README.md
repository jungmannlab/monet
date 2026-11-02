# Reproducibility-probe results

Commit a rig run's output here (one `run_<timestamp>/` directory with its CSVs
and PNGs) so it can be reviewed and analyzed back in the repo.

```bash
# on the rig
python docs/diagnostics/reproducibility_probe.py MyScope --laser 488 \
    --laser-power 100 --cycles 8 --cycle-interval 600 --plot
# -> docs/diagnostics/results/run_YYYYmmdd_HHMMSS/{*.csv,*.png}

git add docs/diagnostics/results/run_YYYYmmdd_HHMMSS
git commit -m "diagnostics: <scope> reproducibility run <date>"
git push
```

Then share the branch/commit and the CSVs can be analyzed here. See
`../README.md` for what each experiment isolates and how to read the plots.
