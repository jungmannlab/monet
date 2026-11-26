<#
.SYNOPSIS
  Build the standalone Windows monet GUI bundle WITHOUT PyInstaller.

.DESCRIPTION
  Assembles dist\monet-gui\ from the python.org *embeddable* CPython
  distribution plus a real `pip install` of monet and its extras. The only
  executables shipped are python.exe / pythonw.exe (Authenticode-signed by the
  Python Software Foundation, with enormous prevalence) and vendor DLLs — there
  is NO custom unsigned PE (no PyInstaller bootloader), which is what Trend
  Micro Apex One's TrendX machine-learning engine was false-positiving as
  `Troj.Win32.TRX.*` and quarantining (first Scripts\monet.exe, then
  monet-gui.exe).

  The GUI launches via `pythonw.exe monet_gui.py` (the same launcher the old
  frozen build used — it sets MONET_HOME / MONET_LOG_DIR and the CWD before
  importing monet). `python.exe monet_gui.py --selfcheck` verifies the bundle.

.NOTES
  Run from the repo root on a Windows runner with a `python` on PATH (used only
  to pin the matching embeddable version — the bundle itself is self-contained).
#>
param(
    [string]$Dest = "dist\monet-gui",
    # Empty => detect from the `python` on PATH so the embeddable version always
    # matches a real, published release (every final CPython has an embed zip).
    [string]$PyVersion = "",
    [string]$Arch = "amd64"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $PyVersion) {
    $raw = (& python --version) 2>&1
    if ($raw -match '(\d+\.\d+\.\d+)') { $PyVersion = $Matches[1] }
    else { throw "Could not detect Python version from: $raw" }
}
Write-Host "Embeddable CPython version: $PyVersion ($Arch)"

$root = (Get-Location).Path
$DestFull = Join-Path $root $Dest
if (Test-Path $DestFull) { Remove-Item -Recurse -Force $DestFull }
New-Item -ItemType Directory -Force -Path $DestFull | Out-Null

# 1. Fetch + extract the embeddable interpreter.
$embedUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-$Arch.zip"
$zip = Join-Path $env:TEMP "python-embed-$PyVersion.zip"
Write-Host "Downloading $embedUrl"
Invoke-WebRequest -Uri $embedUrl -OutFile $zip
Expand-Archive -Path $zip -DestinationPath $DestFull -Force

# 2. Enable site-packages. The embeddable ships python<XY>._pth with
#    `import site` commented out and no site-packages on sys.path; pip-installed
#    packages are invisible until we fix that.
$pthFile = Get-ChildItem -Path $DestFull -Filter "python*._pth" | Select-Object -First 1
if (-not $pthFile) { throw "no python*._pth in the embeddable distribution" }
$lines = @(Get-Content $pthFile.FullName | ForEach-Object { $_ -replace '^\s*#\s*import site\s*$', 'import site' })
if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
if ($lines -notcontains 'import site') { $lines += 'import site' }
Set-Content -Path $pthFile.FullName -Value $lines -Encoding Ascii
Write-Host "Patched $($pthFile.Name):`n$($lines -join "`n")"

$py = Join-Path $DestFull "python.exe"

# 3. Bootstrap pip into the embeddable interpreter (it ships without pip /
#    ensurepip). With `import site` enabled above, get-pip installs cleanly.
$getpip = Join-Path $env:TEMP "get-pip.py"
Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
& $py $getpip --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed ($LASTEXITCODE)" }

# 4. Build every wheel (monet + its full dependency closure, including the
#    git-sourced pycobolt) with the RUNNER's full Python — pip build isolation
#    and setuptools work there, but NOT inside the restricted embeddable
#    interpreter (its ._pth sandbox breaks build isolation). Then install those
#    prebuilt wheels into the embeddable with --no-index / --no-build-isolation,
#    so it never needs a build backend.
$wheelhouse = Join-Path $root "dist\wheelhouse"
if (Test-Path $wheelhouse) { Remove-Item -Recurse -Force $wheelhouse }
New-Item -ItemType Directory -Force -Path $wheelhouse | Out-Null
& python -m pip wheel ".[gui,hardware,setup]" --wheel-dir $wheelhouse
if ($LASTEXITCODE -ne 0) { throw "pip wheel (runner Python) failed ($LASTEXITCODE)" }
& $py -m pip install --no-warn-script-location --no-index --find-links $wheelhouse `
    "monet[gui,hardware,setup]"
if ($LASTEXITCODE -ne 0) { throw "pip install into embeddable failed ($LASTEXITCODE)" }

# 4b. Strip any launcher .exe stubs pip created (get-pip's Scripts\pip.exe, any
#     dependency console scripts) — those are the same unsigned-PE class we're
#     eliminating. Keep only the PSF-signed interpreters.
Get-ChildItem -Path $DestFull -Recurse -Filter *.exe |
    Where-Object { $_.Name -notin @('python.exe', 'pythonw.exe') } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 5. Drop the GUI launcher, a double-clickable wrapper for the portable zip,
#    and the app icon next to the interpreter.
Copy-Item (Join-Path $root "packaging\monet_gui.py") (Join-Path $DestFull "monet_gui.py") -Force
Copy-Item (Join-Path $root "monet\icons\monet.ico") (Join-Path $DestFull "monet.ico") -Force
$cmd = @(
    '@echo off',
    'rem Launch the monet GUI with the bundled (signed) interpreter, no console.',
    'start "" "%~dp0pythonw.exe" "%~dp0monet_gui.py" %*'
)
Set-Content -Path (Join-Path $DestFull "Monet GUI.cmd") -Value $cmd -Encoding Ascii

Write-Host "Built embeddable bundle at $DestFull"
