# bootstrap.ps1 -- from a fresh clone to the app's Setup guide (Windows). Linux, macOS, WSL: bootstrap.sh.
#
#     powershell -ExecutionPolicy Bypass -File bootstrap.ps1            # virtual environment, packages, build and open the app
#     powershell -ExecutionPolicy Bypass -File bootstrap.ps1 -NoGui     # stop before Rust and the app (command line only)
#
# Needs no administrator rights. When a tool is missing it prints the install command and stops; run that, open a
# new PowerShell, then run this again. Safe to rerun. An active virtual environment is used instead of .venv.
param([switch]$NoGui)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Missing($what, $command) {
    Write-Host "Missing: $what. Install it, open a new PowerShell, then run this again:"
    Write-Host "    $command"
    exit 1
}
function Check-Exit($what) {
    if ($LASTEXITCODE -ne 0) { Write-Host "Failed: $what"; exit 1 }
}

if ($env:VIRTUAL_ENV) { $venv = $env:VIRTUAL_ENV } else { $venv = Join-Path $PWD ".venv" }
$py = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $py)) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Missing "Python 3.10 or newer" "winget install Python.Python.3.12"
    }
    python -c "import sys; sys.exit(sys.version_info < (3, 10))"
    if ($LASTEXITCODE -ne 0) { Missing "Python 3.10 or newer" "winget install Python.Python.3.12" }
    Write-Host "== virtual environment: $venv"
    python -m venv $venv
    Check-Exit "python -m venv"
}

Write-Host "== Python packages"
& $py -m pip install --quiet --upgrade pip
Check-Exit "pip upgrade"
& $py -m pip install --quiet -e .
Check-Exit "pip install"

# Before the database load, or the opinion text lands beside the main database.
New-Item -ItemType Directory -Force -Path xml, bulk, db | Out-Null

# So the app runs the same interpreter.
$env:PYTHONPATH = "src"
& $py -m tribonian setup set gui.python $py
Check-Exit "writing the config file"
Remove-Item Env:PYTHONPATH

if ($NoGui) {
    Write-Host "== done. Next: $venv\Scripts\tribonian setup"
    exit 0
}

if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    Missing "Rust" "the installer from https://rustup.rs/ (accept its Visual Studio C++ build tools)"
}

Write-Host "== building and opening the app; its Setup guide is on the start screen"
Set-Location gui
cargo run --release
exit $LASTEXITCODE
