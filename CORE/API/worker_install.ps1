# MOD Worker Install Script (Windows)
#
# This script is served by the API at /workers/agent/install-script (for a
# download token minted with os_name=windows) — the Windows sibling of
# worker_install.sh. A platform admin generates a one-time install command
# from the Dashboard (Workers -> Add Remote Worker):
#
#   $env:MOD_API_URL='https://api.example.com'
#   $env:MOD_BOOTSTRAP_TOKEN='ENROLLMENT_TOKEN'
#   iex (irm 'https://api.example.com/workers/agent/install-script?token=TOKEN')
#
# or, run directly with named parameters:
#
#   .\worker_install.ps1 -ApiUrl 'https://api.example.com' -Token 'TOKEN' `
#       -BootstrapToken 'ENROLLMENT_TOKEN'
#
# PowerShell has no equivalent of `curl | bash -s -- --flag value` for
# piping named arguments through `irm | iex`, so every parameter also falls
# back to an MOD_* environment variable when its -Param is left unset. The
# download token is short-lived (15 min) and single-use, same as the bash
# installer.
#
# Requires: Administrator PowerShell (writes to Program Files, registers a
# service). Run as Administrator or this will fail with an access-denied
# error from New-Item / sc.exe.
#
# NON-ROOT (GSEC Q21.5): the Windows service runs under the default
# LocalSystem service account (not the interactive admin's token) — the
# platform's "non-root where possible" equivalent. The env file is ACL-restricted
# to SYSTEM + Administrators below and the install dir is per-product
# (C:\Program Files\MOD Worker); root-equivalent state on Windows is
# documented, not changed, in this release.

param(
    [string]$Mode            = $(if ($env:MOD_WORKER_MODE)   { $env:MOD_WORKER_MODE }   else { "remote" }),
    [string]$ApiUrl          = $(if ($env:MOD_API_URL)       { $env:MOD_API_URL }       else { "" }),
    [string]$Token           = $(if ($env:MOD_DOWNLOAD_TOKEN){ $env:MOD_DOWNLOAD_TOKEN }else { "" }),
    [string]$BootstrapToken  = $(if ($env:MOD_BOOTSTRAP_TOKEN){ $env:MOD_BOOTSTRAP_TOKEN }else { "" }),
    [string]$VaultAddr       = $(if ($env:MOD_VAULT_ADDR)    { $env:MOD_VAULT_ADDR }    else { "" }),
    [string]$VaultRoleId     = $(if ($env:MOD_VAULT_ROLE_ID) { $env:MOD_VAULT_ROLE_ID } else { "" }),
    [string]$VaultSecretId   = $(if ($env:MOD_VAULT_SECRET_ID){ $env:MOD_VAULT_SECRET_ID }else { "" }),
    [string]$RedisUrl        = $(if ($env:MOD_REDIS_URL)     { $env:MOD_REDIS_URL }     else { "" }),
    [string]$WorkerName      = $(if ($env:MOD_WORKER_NAME)   { $env:MOD_WORKER_NAME }   else { $env:COMPUTERNAME }),
    [string]$InstallDir      = "C:\Program Files\MOD Worker",
    [string]$ConfigDir       = "C:\ProgramData\mod-worker",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ServiceName = "MODWorker"
$DisplayName = "MOD Worker Agent"
$BinaryName  = "mod-worker.exe"
$OS   = "windows"
$Arch = "amd64"

function Assert-Admin {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Error "This script must be run as Administrator (it registers a Windows service)."
        exit 1
    }
}

function Remove-ExistingService {
    param([string]$Name)
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "==> Stopping existing $Name service"
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        # SCM sometimes needs a beat to release the service before delete/recreate.
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Service -Name $Name -ErrorAction SilentlyContinue).Status -eq 'Running' -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
        }
        sc.exe delete $Name | Out-Null
        Start-Sleep -Seconds 2
    }
}

Assert-Admin

if ($Uninstall) {
    Write-Host "==> Uninstalling $ServiceName"
    Remove-ExistingService -Name $ServiceName
    if (Test-Path $InstallDir) {
        Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $ConfigDir) {
        Remove-Item -Path $ConfigDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "==> $ServiceName uninstalled"
    exit 0
}

Write-Host "==> Detected ${OS}/${Arch}"

if ([string]::IsNullOrWhiteSpace($ApiUrl)) {
    Write-Error "ApiUrl is required (-ApiUrl, or `$env:MOD_API_URL when piped through irm | iex)"
    exit 1
}
if ([string]::IsNullOrWhiteSpace($Token)) {
    Write-Error "Token is required (-Token, or `$env:MOD_DOWNLOAD_TOKEN). Generate one from the Dashboard (Workers -> Add Remote Worker) or via API: POST /workers/agent/download-token (platform admin)"
    exit 1
}

# Create install directory (idempotent — re-running just overwrites the binary/config).
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}

# Download the worker binary using the short-lived token — same two-step
# token flow as worker_install.sh (POST /agent/download-token mints Token,
# then GET /agent/download?token=... redeems it once).
$binaryPath = Join-Path $InstallDir $BinaryName
$tmpPath = "$binaryPath.download"
$downloadUrl = "$ApiUrl/workers/agent/download?token=$Token&os_name=$OS&arch=$Arch"
Write-Host "==> Downloading worker agent for ${OS}/${Arch}..."
try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $tmpPath -UseBasicParsing
} catch {
    $statusCode = $null
    if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode }
    Write-Error "Download failed (HTTP $statusCode): $($_.Exception.Message)"
    Remove-Item -Path $tmpPath -Force -ErrorAction SilentlyContinue
    exit 1
}

if (-not (Test-Path $tmpPath) -or (Get-Item $tmpPath).Length -eq 0) {
    Write-Error "Download failed: empty response body"
    Remove-Item -Path $tmpPath -Force -ErrorAction SilentlyContinue
    exit 1
}

# Idempotent re-run: stop the service before replacing the binary in place
# (Windows won't let you overwrite a running exe's file).
$existingSvc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingSvc -and $existingSvc.Status -eq 'Running') {
    Write-Host "==> Stopping $ServiceName to update binary"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
}
Move-Item -Path $tmpPath -Destination $binaryPath -Force
Write-Host "==> Installed to $binaryPath"

# Write environment config. Windows services don't support an
# `EnvironmentFile=` directive like systemd, so this file is informational
# (and available to anything that wants to read it) while the values that
# actually reach the running worker process are written into the service's
# own per-service Environment registry value below — the native equivalent.
$envPath = Join-Path $ConfigDir "worker.env"
$envLines = @(
    "WORKER_NAME=$WorkerName"
    "WORKER_MODE=$Mode"
    "API_URL=$ApiUrl"
    "BOOTSTRAP_TOKEN=$BootstrapToken"
    "VAULT_ADDR=$VaultAddr"
    "VAULT_APPROLE_ROLE_ID=$VaultRoleId"
    "VAULT_APPROLE_SECRET_ID=$VaultSecretId"
    "REDIS_URL=$RedisUrl"
)
Set-Content -Path $envPath -Value $envLines
# Best-effort lock-down: restrict to Administrators + SYSTEM (no secrets echoed to the console above).
try {
    icacls $envPath /inheritance:r /grant:r "SYSTEM:F" "BUILTIN\Administrators:F" | Out-Null
} catch {
    Write-Host "==> Note: could not restrict ACLs on $envPath (icacls unavailable); file still written."
}

# Register/refresh the Windows service.
Remove-ExistingService -Name $ServiceName

Write-Host "==> Creating Windows service"
$quotedBin = '"' + $binaryPath + '"'
sc.exe create $ServiceName binPath= $quotedBin start= auto DisplayName= $DisplayName | Out-Null
sc.exe description $ServiceName "MOD Worker Agent - processes tasks from the MOD platform" | Out-Null

# Recovery actions: the worker's fetch-loop watchdog (main.go
# startFetchLoopWatchdog) intentionally os.Exit(1)s a green-but-silent
# worker rather than trying to self-heal in-process — it relies on the
# service supervisor to restart it. reset= 86400 clears the failure count
# after 24h of continuous health so a flapping restart loop doesn't get
# stuck at the longest backoff tier forever.
#
# SOURCE OF TRUTH for the tier schedule (drift resolved 2026-10-07, WIN.1.s4):
# the ESCALATING schedule restart/10000/restart/30000/restart/60000, which
# CORE/Worker/scripts/install.ps1:66 and the watchdog comment at
# CORE/Worker/cmd/worker/main.go:2234 already document ("first after 10s, then
# 30s, then 60s"). The flat 5000x3 this line used before the fix was the odd
# one out: it restarted a green-but-silent worker every 5s, which hammers the
# API (the watchdog exists precisely to stop that storm), and its first tier
# disagreed with the Linux sibling's RestartSec=10 (CORE/Worker/scripts/install.sh:117).
sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/30000/restart/60000 | Out-Null
# failureflag=1 makes SCM apply the failure actions to a non-zero exit too
# (not just crashes) — the watchdog exits with os.Exit(1), a "clean" process
# exit as far as Windows is concerned, which SCM otherwise treats as a
# deliberate stop and won't auto-restart.
sc.exe failureflag $ServiceName 1 | Out-Null

# Per-service environment — the Windows-native analogue of systemd's
# `EnvironmentFile=`. Values here are what the running worker process
# actually sees (secrets are written to the registry, not echoed to stdout).
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
New-ItemProperty -Path $regPath -Name "Environment" -PropertyType MultiString -Value $envLines -Force | Out-Null

Write-Host "==> Starting $ServiceName"
Start-Service -Name $ServiceName

Write-Host ""
Write-Host "==> $ServiceName installed and started"
Write-Host "    Check status: Get-Service $ServiceName"
Write-Host "    View logs:    Get-EventLog -LogName Application -Source $ServiceName"
Write-Host "    Uninstall:    .\worker_install.ps1 -Uninstall"
