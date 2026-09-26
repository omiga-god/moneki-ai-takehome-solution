$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$candidates = @(
    (Join-Path $root 'starter\.venv\Scripts\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
)
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand) { $candidates += $pythonCommand.Source }
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($launcher) {
    $found = & $launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0) { $candidates += $found }
}
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $candidate) {
        & $candidate -c 'import sys, venv; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $env:PYTHONUTF8 = '1'
            & $candidate (Join-Path $PSScriptRoot 'launch.py') @args
            exit $LASTEXITCODE
        }
    }
}
Write-Host 'Python 3.12 (full installation) is required. Install it, then double-click start.bat again.'
Write-Host 'winget install --source winget --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements'
exit 1
