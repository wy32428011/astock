param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$WebRoot = Join-Path $Root "web"
$LogDir = Join-Path $Root "logs"
$OutputDir = Join-Path $Root "output\playwright"
New-Item -ItemType Directory -Force -Path $LogDir, $OutputDir | Out-Null

# 从 .env 中读取数据库连接配置，避免验收脚本和应用配置漂移。
function Get-EnvValue {
    param(
        [string]$Name,
        [string]$DefaultValue
    )
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) {
        return $DefaultValue
    }
    foreach ($line in (Get-Content $envFile)) {
        if ($line -match "^\s*$Name\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim()
        }
    }
    return $DefaultValue
}

# 检查 MySQL 端口是否可达，决定执行真实数据验收还是离线降级验收。
function Test-MySqlPort {
    $mysqlHost = Get-EnvValue "MYSQL_HOST" "127.0.0.1"
    $mysqlPort = [int](Get-EnvValue "MYSQL_PORT" "3306")
    $timeoutMs = 8000
    $client = New-Object System.Net.Sockets.TcpClient
    $connected = $false
    try {
        $async = $client.BeginConnect($mysqlHost, $mysqlPort, $null, $null)
        if ($async.AsyncWaitHandle.WaitOne($timeoutMs)) {
            $client.EndConnect($async)
            $connected = $true
        }
    }
    catch {
        $connected = $false
    }
    $client.Close()
    return $connected
}

# 停止本脚本启动的 API 和前端进程，避免残留占用端口。
function Stop-FrontendVerificationProcesses {
    $patterns = @(
        "*astocks-collector*serve-api*--host 127.0.0.1 --port $ApiPort*",
        "*vite*--host 127.0.0.1 --port $WebPort*"
    )
    $processes = @()
    foreach ($candidate in (Get-CimInstance Win32_Process)) {
        $commandLine = $candidate.CommandLine
        $name = $candidate.Name
        $matched = $false
        foreach ($pattern in $patterns) {
            if ($commandLine -like $pattern) {
                $matched = $true
            }
        }
        if ($name -ne "pwsh.exe" -and $matched) {
            $processes += $candidate
        }
    }
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# 校验接口在数据库不可达时返回 503。
function Assert-UnavailableEndpoint {
    param([string]$Path)
    try {
        Invoke-RestMethod "http://127.0.0.1:$ApiPort$Path" | Out-Null
        throw "Endpoint $Path should be unavailable while MySQL is offline"
    }
    catch {
        $statusCode = $null
        if ($_.Exception.Response -ne $null) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($statusCode -ne 503) {
            throw "Endpoint $Path returned unexpected status $statusCode while MySQL is offline"
        }
        Write-Host "Offline endpoint $Path returned 503 as expected"
    }
}

# 校验接口在数据库可达时返回真实业务数据。
function Assert-OnlineApiData {
    $overview = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/overview"
    if ($overview.stock_count -le 0 -or $overview.daily_count -le 0 -or -not $overview.latest_trade_date) {
        throw "Overview API returned incomplete data"
    }

    $stocks = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/stocks?page=1&page_size=5"
    if ($stocks.total -le 0 -or $stocks.data.Count -le 0) {
        throw "Stocks API returned empty data"
    }

    $segments = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/segments"
    if (
        $segments.exchangeDistribution.Count -le 0 -or
        $segments.riskDistribution.Count -le 0 -or
        $segments.pctDistribution.Count -le 0 -or
        $segments.amountTop.Count -le 0
    ) {
        throw "Segments API returned incomplete data"
    }

    $history = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/history/000001?days=120"
    if ($history.stock.symbol -ne "000001" -or $history.data.Count -le 0) {
        throw "History API returned incomplete data"
    }

    $analysis = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/analysis"
    if ($analysis.data.Count -le 0 -or -not $analysis.analysisDate) {
        throw "Analysis API returned empty data"
    }

    return [pscustomobject]@{
        Overview = $overview
        FirstPickName = $analysis.data[0].name
        FirstPickSymbol = $analysis.data[0].symbol
    }
}

$failure = $null
try {
    Stop-FrontendVerificationProcesses

    if (-not $SkipBuild) {
        Push-Location $Root
        python -m compileall src
        Pop-Location

        Push-Location $WebRoot
        npm run build
        Pop-Location
    }

    $mysqlAvailable = Test-MySqlPort
    $mode = if ($mysqlAvailable) { "online" } else { "offline" }
    Write-Host "MySQL reachable: $mysqlAvailable; Playwright mode: $mode"

    $apiOut = Join-Path $LogDir "frontend-api.out.log"
    $apiErr = Join-Path $LogDir "frontend-api.err.log"
    $webOut = Join-Path $LogDir "frontend-web.out.log"
    $webErr = Join-Path $LogDir "frontend-web.err.log"

    $api = Start-Process -FilePath powershell.exe `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "astocks-collector serve-api --host 127.0.0.1 --port $ApiPort" `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $apiOut `
        -RedirectStandardError $apiErr `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path (Join-Path $LogDir "frontend-api.pid") -Value $api.Id

    $web = Start-Process -FilePath powershell.exe `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "npx vite --host 127.0.0.1 --port $WebPort" `
        -WorkingDirectory $WebRoot `
        -RedirectStandardOutput $webOut `
        -RedirectStandardError $webErr `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path (Join-Path $LogDir "frontend-web.pid") -Value $web.Id

    Start-Sleep -Seconds 6

    $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/health"
    if ($health.status -ne "ok") {
        throw "API health check failed"
    }

    $expectedDate = ""
    $expectedPick = ""
    $expectedPickSymbol = ""
    if ($mysqlAvailable) {
        $onlineData = Assert-OnlineApiData
        $expectedDate = $onlineData.Overview.latest_trade_date
        $expectedPick = $onlineData.FirstPickName
        $expectedPickSymbol = $onlineData.FirstPickSymbol
        Write-Host "Overview stock_count=$($onlineData.Overview.stock_count) latest_trade_date=$expectedDate"
        Write-Host "Analysis first pick=$expectedPickSymbol"
    }
    else {
        Assert-UnavailableEndpoint "/api/overview"
        Assert-UnavailableEndpoint "/api/stocks?page=1&page_size=5"
        Assert-UnavailableEndpoint "/api/segments"
        Assert-UnavailableEndpoint "/api/history/000001?days=120"
        Assert-UnavailableEndpoint "/api/analysis"
    }

    Push-Location $WebRoot
    node scripts\verify_frontend.mjs `
        --base-url "http://127.0.0.1:$WebPort" `
        --mode $mode `
        --out-dir $OutputDir `
        --prefix "verify-$mode" `
        --expected-date $expectedDate `
        --expected-pick $expectedPick `
        --expected-pick-symbol $expectedPickSymbol
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright verification failed with exit code $LASTEXITCODE"
    }
    Pop-Location
}
catch {
    $failure = $_
}

Stop-FrontendVerificationProcesses
Start-Sleep -Seconds 2
$remaining = @()
foreach ($candidate in (Get-CimInstance Win32_Process)) {
    if (
        $candidate.Name -ne "pwsh.exe" -and
        (
            $candidate.CommandLine -like "*astocks-collector*serve-api*--host 127.0.0.1 --port $ApiPort*" -or
            $candidate.CommandLine -like "*vite*--host 127.0.0.1 --port $WebPort*"
        )
    ) {
        $remaining += $candidate
    }
}
$ports = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $ApiPort, $WebPort -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" }
Write-Host "Remaining verification processes: $(($remaining | Measure-Object).Count)"
Write-Host "Remaining listening ports: $(($ports | Measure-Object).Count)"

if ($failure -ne $null) {
    throw $failure
}
