# start_opend.ps1 - Launch both CLI OpenD instances (AU + SG).
# Password is hardcoded in C:\moomoo_opend\au\OpenD.xml and C:\moomoo_opend\sg\OpenD.xml
#
# Accounts:
#   AU - phone +61 470625861 - port 11111 - telnet 22221 - acc_id 284008279318020118
#   SG - email karthiyuvi99@gmail.com - port 11112 - telnet 22222 - acc_id 283726805871679562
#
# Usage:
#   .\start_opend.ps1           - start both
#   .\start_opend.ps1 -SkipSG  - start AU only

param([switch]$SkipSG)

$AuDir    = "C:\moomoo_opend\au"
$SgDir    = "C:\moomoo_opend\sg"
$AuExe    = "$AuDir\OpenD.exe"
$SgExe    = "$SgDir\OpenD.exe"
$AuApiPort = 11111
$SgApiPort = 11112
$AuTelnet  = 22221
$SgTelnet  = 22222

# --- Verify CLI installs -------------------------------------------------
foreach ($exe in @($AuExe, $SgExe)) {
    if (-not (Test-Path $exe)) {
        Write-Error "CLI OpenD not found: $exe"
        exit 1
    }
}

# --- Kill any running OpenD processes ------------------------------------
$toKill = @()
$toKill += Get-Process -Name "moomoo_OpenD" -ErrorAction SilentlyContinue
$toKill += Get-Process -Name "OpenD"        -ErrorAction SilentlyContinue
if ($toKill.Count -gt 0) {
    Write-Host "Stopping $($toKill.Count) existing OpenD process(es)..." -ForegroundColor Yellow
    $toKill | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# --- Helper: send command to OpenD telnet console ------------------------
function Send-OpenDTelnet {
    param([int]$TelnetPort, [string]$Command)
    try {
        $tcp  = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", $TelnetPort)
        $stream = $tcp.GetStream()
        Start-Sleep -Milliseconds 300
        # drain any welcome bytes
        if ($stream.DataAvailable) {
            $buf = New-Object byte[] 1024
            $stream.Read($buf, 0, 1024) | Out-Null
        }
        $bytes = [System.Text.Encoding]::ASCII.GetBytes("$Command`r`n")
        $stream.Write($bytes, 0, $bytes.Length)
        Start-Sleep -Milliseconds 400
        $resp = ""
        if ($stream.DataAvailable) {
            $buf = New-Object byte[] 4096
            $n = $stream.Read($buf, 0, 4096)
            $resp = [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
        }
        $tcp.Close()
        return $resp
    } catch {
        return ""
    }
}

# --- Helper: check if port is listening ----------------------------------
function Test-Port {
    param([int]$Port)
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", $Port)
        $tcp.Close()
        return $true
    } catch { return $false }
}

# --- Helper: find latest captcha image for an account --------------------
function Get-CaptchaImagePath {
    param([string]$LoginAccount)
    # OpenD saves captchas under AppData\Roaming\com.moomoo.OpenD\<hash>\PicVerif\
    $base = "$env:APPDATA\com.moomoo.OpenD"
    if (-not (Test-Path $base)) { return $null }
    $img = Get-ChildItem $base -Recurse -Include "*.png","*.jpg","*.bmp","*.gif" `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    return $img
}

# --- Helper: watch log for a pattern (returns $true if found) -----------
function Wait-ForLog {
    param([string]$LogDir, [string]$Pattern, [int]$TimeoutSec = 20)
    $end = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $end) {
        $logs = Get-ChildItem "$LogDir\Log" -Filter "*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($logs) {
            $content = Get-Content $logs.FullName -Tail 30 -ErrorAction SilentlyContinue
            if ($content -match $Pattern) { return $true }
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# --- Launch an instance and handle captcha if needed ---------------------
function Start-OpenDAndLogin {
    param(
        [string]$Label,
        [string]$Exe,
        [string]$Dir,
        [int]$ApiPort,
        [int]$TelnetPort,
        [string]$LogDir,
        [string]$Account
    )

    Write-Host "Starting $Label OpenD (port $ApiPort)..." -ForegroundColor Green
    Start-Process -FilePath $Exe -WorkingDirectory $Dir -WindowStyle Minimized

    # Wait for startup and login attempt
    Write-Host "  Waiting for $Label to log in (up to 30s)..." -ForegroundColor Cyan
    $loggedIn = $false
    $needsCaptcha = $false
    $end = (Get-Date).AddSeconds(30)

    while ((Get-Date) -lt $end) {
        Start-Sleep -Milliseconds 800
        $logs = Get-ChildItem "$LogDir\Log" -Filter "*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $logs) { continue }
        $tail = (Get-Content $logs.FullName -Tail 40 -ErrorAction SilentlyContinue) -join " "

        if ($tail -match "Login success|Qot authority|login success") {
            $loggedIn = $true; break
        }
        if ($tail -match "Login failed,Password does not match") {
            Write-Host "  $Label - PASSWORD MISMATCH" -ForegroundColor Red
            break
        }
        if ($tail -match "req_pic_verify_code|graphic verification") {
            $needsCaptcha = $true
            Write-Host "  $Label - Captcha required!" -ForegroundColor Yellow
            break
        }
        if ($tail -match "Login failed") {
            Write-Host "  $Label - Login failed (unknown reason)" -ForegroundColor Red
            break
        }
    }

    # Handle captcha
    if ($needsCaptcha) {
        # Find and open the captcha image
        Start-Sleep -Seconds 1
        $img = Get-CaptchaImagePath -LoginAccount $Account
        if ($img) {
            Write-Host "  Opening captcha image: $($img.FullName)" -ForegroundColor Cyan
            Start-Process $img.FullName
        } else {
            $picDir = "$env:APPDATA\com.moomoo.OpenD"
            Write-Host "  Could not auto-find captcha. Check: $picDir" -ForegroundColor Yellow
        }

        $code = Read-Host "  Enter $Label captcha code"
        $resp = Send-OpenDTelnet -TelnetPort $TelnetPort -Command "input_pic_verify_code -code=$code"
        Write-Host "  Telnet response: $resp" -ForegroundColor DarkGray

        # Wait for login to complete after captcha
        $end2 = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $end2) {
            Start-Sleep -Milliseconds 800
            $logs = Get-ChildItem "$LogDir\Log" -Filter "*.log" -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if (-not $logs) { continue }
            $tail = (Get-Content $logs.FullName -Tail 20 -ErrorAction SilentlyContinue) -join " "
            if ($tail -match "Login success|Qot authority|login success") {
                $loggedIn = $true; break
            }
            if ($tail -match "Login failed") {
                Write-Host "  $Label - Login failed after captcha" -ForegroundColor Red; break
            }
        }
    }

    if ($loggedIn) {
        Write-Host "  $Label - Logged in OK" -ForegroundColor Green
    }

    return $loggedIn
}

# --- Launch instances ----------------------------------------------------
$AuLogDir = "D:\workspace\repo\stocks\opend_configs\au\logs"
$SgLogDir = "D:\workspace\repo\stocks\opend_configs\sg\logs"

New-Item -ItemType Directory -Force $AuLogDir | Out-Null
New-Item -ItemType Directory -Force $SgLogDir | Out-Null

$auOk = Start-OpenDAndLogin -Label "AU" -Exe $AuExe -Dir $AuDir `
    -ApiPort $AuApiPort -TelnetPort $AuTelnet `
    -LogDir $AuLogDir -Account "+61 470625861"

Start-Sleep -Seconds 1

$sgOk = $true
if (-not $SkipSG) {
    $sgOk = Start-OpenDAndLogin -Label "SG" -Exe $SgExe -Dir $SgDir `
        -ApiPort $SgApiPort -TelnetPort $SgTelnet `
        -LogDir $SgLogDir -Account "karthiyuvi99@gmail.com"
}

# --- Final port check ----------------------------------------------------
Write-Host ""
Write-Host "Checking API ports..." -ForegroundColor Cyan
$ports = if ($SkipSG) { @(11111) } else { @(11111, 11112) }
foreach ($p in $ports) {
    if (Test-Port $p) {
        Write-Host "  Port $p - READY" -ForegroundColor Green
    } else {
        Write-Host "  Port $p - NOT reachable" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Next: python _probe_accounts.py" -ForegroundColor Cyan
