<#
.SYNOPSIS
    Windows/PowerShell equivalent of the Makefile. Same targets, same defaults.

.USAGE
    .\tasks.ps1 <target> [-File <path>] [-Loss <0..1>] [-Delay <sec>] [-Jitter <sec>]

    Targets: venv, testfiles, test, smoke, experiment, burst, plots, demo, clean, help

.EXAMPLES
    .\tasks.ps1 venv
    .\tasks.ps1 testfiles
    .\tasks.ps1 test
    .\tasks.ps1 smoke
    .\tasks.ps1 experiment
    .\tasks.ps1 burst
    .\tasks.ps1 plots
    .\tasks.ps1 demo -Loss 0.1
    .\tasks.ps1 clean

If `py` isn't on PATH but `python` is (or vice versa), the script auto-detects
whichever launcher is available -- no manual edits needed.
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("venv", "testfiles", "test", "smoke", "experiment", "burst", "plots", "demo", "clean", "help")]
    [string]$Target = "help",

    [string]$File = "test_files/medium.bin",
    [double]$Loss = 0.08,
    [double]$Delay = 0.02,
    [double]$Jitter = 0.005
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Resolve-Python {
    foreach ($cand in @("py", "python", "python3")) {
        if (Get-Command $cand -ErrorAction SilentlyContinue) { return $cand }
    }
    throw "No Python launcher found on PATH (tried py, python, python3)."
}

$PY = Resolve-Python
$VENV_PY = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Get-VenvPython {
    if (Test-Path $VENV_PY) { return $VENV_PY }
    Write-Warning ".venv not found -- run '.\tasks.ps1 venv' first. Falling back to '$PY'."
    return $PY
}

switch ($Target) {
    "help" {
        Write-Host "Targets: venv, testfiles, test, smoke, experiment, burst, plots, demo, clean"
        Write-Host "  venv        - create .venv and install matplotlib"
        Write-Host "  testfiles   - generate test_files\*.bin"
        Write-Host "  test        - unit-test common\ modules"
        Write-Host "  smoke       - one baseline + one improved transfer with integrity check"
        Write-Host "  experiment  - loss-rate sweep (both protocols) -> results\"
        Write-Host "  burst       - recovery-after-loss-burst experiment -> results\"
        Write-Host "  plots       - regenerate all graphs from results\"
        Write-Host "  demo        - live side-by-side comparison (-File/-Loss/-Delay/-Jitter)"
        Write-Host "  clean       - delete generated results\ files"
        Write-Host ""
        Write-Host "Options apply to smoke/experiment/demo: -File <path> -Loss <0..1> -Delay <sec> -Jitter <sec>"
    }

    "venv" {
        & $PY -m venv .venv
        & $VENV_PY -m pip install -q --upgrade pip matplotlib
        Write-Host "venv ready: $VENV_PY"
    }

    "testfiles" {
        & $PY test_files\make_test_files.py
    }

    "test" {
        & $PY -m experiments.test_common
    }

    "smoke" {
        & $PY -m experiments.run_transfer --proto baseline --file $File --loss $Loss --delay $Delay --jitter $Jitter
        & $PY -m experiments.run_transfer --proto improved --file $File --loss $Loss --delay $Delay --jitter $Jitter
    }

    "experiment" {
        $vpy = Get-VenvPython
        & $vpy -m experiments.run_experiment --file $File --loss 0 0.01 0.05 0.10 --repeats 3 --delay $Delay --jitter $Jitter
    }

    "burst" {
        $vpy = Get-VenvPython
        & $vpy -m experiments.run_experiment --file test_files/large.bin --loss 0.05 --repeats 5 `
            --delay $Delay --jitter 0.006 --burst-at 1.5 --burst-len 0.8 --base-port 5600
    }

    "plots" {
        $vpy = Get-VenvPython
        & $vpy -m experiments.plot_results --cwnd-loss 0.05
    }

    "demo" {
        $vpy = Get-VenvPython
        & $vpy -m experiments.demo --file $File --loss $Loss --delay $Delay --jitter $Jitter
    }

    "clean" {
        Remove-Item -Force -ErrorAction SilentlyContinue results\*.csv, results\*.json, results\*.png, results\*.received.bin
        Write-Host "cleaned results\"
    }
}
