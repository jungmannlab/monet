@echo off
rem monet launcher for Windows — a non-PE replacement for the pip-generated
rem Scripts\monet.exe, which Trend Micro Apex One (TrendX ML) false-positives
rem as "Troj.Win32.TRX.*" and quarantines. This .cmd just forwards to the
rem module entry point, so `monet gui`, `monet serve`, `monet calibrate`, ...
rem all work exactly as before, with no executable to flag.
rem
rem Deploy: copy this file onto PATH (e.g. the environment's Scripts\ dir, or
rem any dir already on PATH). It runs whichever `python` is first on PATH, so
rem activate the monet environment (conda/venv) first, as you would anyway.
python -m monet %*
