# MOD Worker Install Script (Windows)
# Downloads the worker binary and registers it as a Windows service.
#
# Usage (PowerShell as Admin):
#   .\install.ps1 -ApiUrl "https://api.yourdomain.com" -BootstrapToken "YOUR_TOKEN"

param(
    [string]$Mode = "remote",
    [string]$ApiUrl = "",
    [string]$BootstrapToken = "",
    [string]$VaultAddr = "",
    [string]$VaultRoleId = "",
    [string]$VaultSecretId = "",
    [string]$RedisUrl = "",
    [string]$WorkerName = $env:COMPUTERNAME,
    [string]$InstallDir = "C:\Program Files\MOD Worker",
    [string]$ReleaseUrl = "https://releases.yourdomain.com"
)

$ErrorActionPreference = "Stop"
$BinaryName = "mod-worker.exe"

Write-Host "==> Installing MOD Worker for Windows/amd64"

# Create install directory
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

# Download binary
$downloadUrl = "$ReleaseUrl/mod-worker-windows-amd64.exe"
$binaryPath = Join-Path $InstallDir $BinaryName
Write-Host "==> Downloading from $downloadUrl"
Invoke-WebRequest -Uri $downloadUrl -OutFile $binaryPath

# Write environment config
$envPath = Join-Path $InstallDir "worker.env"
@"
WORKER_NAME=$WorkerName
WORKER_MODE=$Mode
API_URL=$ApiUrl
BOOTSTRAP_TOKEN=$BootstrapToken
VAULT_ADDR=$VaultAddr
VAULT_APPROLE_ROLE_ID=$VaultRoleId
VAULT_APPROLE_SECRET_ID=$VaultSecretId
REDIS_URL=$RedisUrl
"@ | Set-Content -Path $envPath

# Register as Windows service using sc.exe
$serviceName = "MODWorker"
$displayName = "MOD Worker Agent"

# Remove existing service if present
$existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "==> Removing existing service"
    Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $serviceName | Out-Null
    Start-Sleep -Seconds 2
}

# Create service
Write-Host "==> Creating Windows service"
sc.exe create $serviceName binPath= "`"$binaryPath`"" start= auto DisplayName= $displayName | Out-Null
sc.exe description $serviceName "MOD Worker Agent - processes tasks from the MOD platform" | Out-Null
sc.exe failure $serviceName reset= 86400 actions= restart/10000/restart/30000/restart/60000 | Out-Null

# Start service
Start-Service -Name $serviceName

Write-Host "==> $serviceName installed and started"
Write-Host "    Check status: Get-Service $serviceName"
Write-Host "    View logs:    Get-EventLog -LogName Application -Source $serviceName"
