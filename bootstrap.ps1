# Run bootstrap.sh with Git Bash, passing every argument through:
#
#   powershell -File bootstrap.ps1 --workspace D:\work --project gui_forms [--dry-run] ...
#
# Git Bash is found beside git.exe on PATH (<Git>\cmd\git.exe -> <Git>\bin\bash.exe),
# else in the default install directory. Without it the script stops and says so.
$ErrorActionPreference = 'Stop'

$bash = $null
$git = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -ne $git) {
    $candidate = Join-Path (Split-Path (Split-Path $git.Source -Parent) -Parent) 'bin\bash.exe'
    if (Test-Path $candidate) { $bash = $candidate }
}
if ($null -eq $bash) {
    $candidate = Join-Path $env:ProgramFiles 'Git\bin\bash.exe'
    if (Test-Path $candidate) { $bash = $candidate }
}
if ($null -eq $bash) {
    Write-Error 'bootstrap: Git Bash (bash.exe from Git for Windows) was not found; install Git for Windows'
    exit 1
}

$script = Join-Path $PSScriptRoot 'bootstrap.sh'
& $bash $script @args
exit $LASTEXITCODE
