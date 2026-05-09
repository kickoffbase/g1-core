# One-shot redeploy from the operator laptop (Windows) to the robot.
#
# Usage (PowerShell):
#   .\scripts\deploy.ps1
#   .\scripts\deploy.ps1 -Robot unitree@10.0.0.42
#   .\scripts\deploy.ps1 -NoRestart
#   .\scripts\deploy.ps1 -RemoteDir /home/unitree/g1-core
#
# Mirrors scripts/deploy.sh for Windows. Uses built-in OpenSSH
# (`ssh`, `scp`) which ship with Windows 10/11. We don't try to use
# rsync because it's not available out of the box.
#
# What it does:
#   1. Tar the local repo (excluding .git, .venv, __pycache__, .env, state).
#   2. Stream the tarball to the robot over ssh and unpack it on the fly,
#      preserving .env and state/ that already exist on the robot.
#   3. Restart the user-systemd unit; preflight.py on the robot handles
#      CRLF stripping + port cleanup + network wait.
#   4. Print status + the last 40 journal lines so you see boot output.
#
# If you don't have `tar` (Windows 10 1803+ ships it under
# C:\Windows\System32\tar.exe), the script falls back to a plain `scp -r`
# of the whole tree.

[CmdletBinding()]
param(
    [string]$Robot = $env:ROBOT,
    [string]$RemoteDir = $env:REMOTE_DIR,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'

if (-not $Robot)     { $Robot = 'unitree@192.168.123.164' }
if (-not $RemoteDir) { $RemoteDir = '/home/unitree/g1-core' }

$LocalDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

Write-Host "==> Target: $Robot`:$RemoteDir"
Write-Host "==> Source: $LocalDir"

$exclude = @('.git', '.venv', '__pycache__', '.env', 'state')
$tarBin = Get-Command tar -ErrorAction SilentlyContinue

if ($tarBin) {
    Write-Host "==> Streaming tar over ssh"

    # Build --exclude flags for the local tar. We do *not* exclude the
    # destination's .env/state — those are protected by extracting with
    # `tar --keep-newer-files` is unsafe (timestamps differ); instead we
    # rely on the local tarball simply not containing those paths, so an
    # `tar -xf` on the remote leaves the existing .env / state alone.
    $tarArgs = @('-czf', '-')
    foreach ($e in $exclude) { $tarArgs += "--exclude=$e" }
    $tarArgs += @('-C', $LocalDir, '.')

    # PowerShell pipe: tar | ssh "tar -xz -C remoteDir"
    & $tarBin.Path @tarArgs | & ssh $Robot "mkdir -p '$RemoteDir' && tar -xzf - -C '$RemoteDir'"
    if ($LASTEXITCODE -ne 0) {
        throw "tar|ssh failed with exit code $LASTEXITCODE"
    }
}
else {
    Write-Host "==> tar not found, falling back to scp -r (slower)"
    # scp doesn't honour excludes; we just push everything except .git
    # by copying piece-by-piece from a temp staging directory.
    $stage = Join-Path $env:TEMP "g1-core-deploy-$(Get-Random)"
    New-Item -ItemType Directory -Path $stage | Out-Null
    try {
        $excludePatterns = $exclude | ForEach-Object { "*$_*" }
        Get-ChildItem -Path $LocalDir -Force | ForEach-Object {
            if ($exclude -notcontains $_.Name) {
                Copy-Item -Path $_.FullName -Destination $stage -Recurse -Force
            }
        }
        & ssh $Robot "mkdir -p '$RemoteDir'"
        if ($LASTEXITCODE -ne 0) { throw "ssh mkdir failed ($LASTEXITCODE)" }
        & scp -r "$stage\*" "${Robot}:${RemoteDir}/"
        if ($LASTEXITCODE -ne 0) { throw "scp failed ($LASTEXITCODE)" }
    }
    finally {
        Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    }
}

if ($NoRestart) {
    Write-Host "==> -NoRestart: skipping restart"
    exit 0
}

Write-Host "==> systemctl --user restart g1-core"
$remoteScript = @'
set -e
systemctl --user restart g1-core
sleep 2
echo
systemctl --user --no-pager status g1-core | head -n 10
echo "--- journalctl --user -u g1-core -n 40 --no-pager ---"
journalctl --user -u g1-core -n 40 --no-pager
'@

& ssh $Robot $remoteScript
if ($LASTEXITCODE -ne 0) {
    throw "ssh restart failed ($LASTEXITCODE)"
}
