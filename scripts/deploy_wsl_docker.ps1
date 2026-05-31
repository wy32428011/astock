param(
    [int]$Port = 8003
)

# Deploy astocks collector into local WSL Docker.
$ErrorActionPreference = "Stop"

function Convert-ToWslPath {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)

    # Convert a Windows drive path to a WSL /mnt path for docker compose.
    $resolvedPath = (Resolve-Path -LiteralPath $WindowsPath).ProviderPath
    if ([string]::IsNullOrWhiteSpace($resolvedPath) -or $resolvedPath.Length -lt 3) {
        throw "Cannot resolve project path: $WindowsPath"
    }
    $drive = $resolvedPath.Substring(0, 1).ToLowerInvariant()
    $pathPart = $resolvedPath.Substring(2).Replace("\", "/")
    return "/mnt/$drive$pathPart"
}

function Quote-Bash {
    param([Parameter(Mandatory = $true)][string]$Value)

    # Quote Bash arguments safely so spaces in paths do not break commands.
    return "'" + $Value.Replace("'", "'""'""'") + "'"
}

function Invoke-WslBash {
    param([Parameter(Mandatory = $true)][string]$Command)

    wsl -u root bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL Docker command failed: $Command"
    }
}

function Resolve-ProjectRoot {
    # Find the project root from script paths and current location.
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        $candidates += Join-Path $PSScriptRoot ".."
    }
    if (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
        $candidates += Join-Path (Split-Path -Parent $PSCommandPath) ".."
    }
    if (-not [string]::IsNullOrWhiteSpace($MyInvocation.MyCommand.Path)) {
        $candidates += Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) ".."
    }
    $candidates += (Get-Location).ProviderPath

    foreach ($candidate in $candidates) {
        $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
        if ($null -eq $resolved) {
            continue
        }
        $path = $resolved.ProviderPath
        if ([string]::IsNullOrWhiteSpace($path)) {
            $path = $resolved.Path
        }
        if ((Test-Path -LiteralPath (Join-Path $path "docker-compose.yml")) -and
            (Test-Path -LiteralPath (Join-Path $path "Dockerfile"))) {
            return $path
        }
    }

    throw "Cannot find astocks collector project root"
}

$projectRoot = Resolve-ProjectRoot
$wslProjectRoot = Convert-ToWslPath $projectRoot
$quotedProjectRoot = Quote-Bash $wslProjectRoot

Write-Host "Checking MySQL container astocks-mysql8..."
Invoke-WslBash "docker ps --format '{{.Names}}' | grep -qx astocks-mysql8"

Write-Host "Preparing Docker network astocks-net..."
Invoke-WslBash "docker network inspect astocks-net >/dev/null 2>&1 || docker network create astocks-net >/dev/null"
Invoke-WslBash "docker network connect astocks-net astocks-mysql8 2>/dev/null || true"

Write-Host "Building and starting astocks-collector-app..."
Invoke-WslBash "cd $quotedProjectRoot && ASTOCKS_APP_PORT=$Port docker compose up -d --build astocks-app"

Write-Host "Current app container status:"
Invoke-WslBash "docker ps --filter name=astocks-collector-app --format '{{.Names}} {{.Status}} {{.Ports}}'"

Write-Host "Deployment complete: http://127.0.0.1:$Port"
