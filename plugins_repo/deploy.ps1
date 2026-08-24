#!/usr/bin/env pwsh
# DualSub plugin hot-deploy script (no MoviePilot container restart)
# Usage: .\deploy.ps1
# Flow: upload code -> sync package.v2.json -> clear __pycache__ -> reload API

$ErrorActionPreference = "Stop"

# === config ===
$nasIp = "192.168.1.57"
$sshUser = "admin"
$sshPass = "Wzdsrs0301"
$container = "moviepilot-v2"
$repoRoot = "C:\Users\ZhenZhenNa\Desktop\web\dualsub-web\plugins_repo"
$pluginDir = "$repoRoot\plugins\dualsub"
$pluginId = "DualSub"
$mpUrl = "http://${nasIp}:3000"
$apiKey = "0ZqtnEL8L8tKrWK4XLfMQQ"

# === 1. SSH connect ===
Import-Module Posh-SSH
$pwSec = ConvertTo-SecureString $sshPass -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential($sshUser, $pwSec)
$s = New-SSHSession -ComputerName $nasIp -Port 22 -Credential $cred -AcceptKey
Write-Host "SSH connected" -ForegroundColor Green

# === 2. upload files via base64 ===
# Targets:
#   /app/app/plugins/dualsub/        (runtime, loaded by MoviePilot)
#   /config/plugins_repo/plugins/dualsub/      (local repo v1 fallback)
#   /config/plugins_repo/plugins.v2/dualsub/   (local repo v2 source)
$targets = @(
    "/app/app/plugins/dualsub",
    "/config/plugins_repo/plugins/dualsub",
    "/config/plugins_repo/plugins.v2/dualsub"
)
$files = @("__init__.py", "merge_dual.py")
foreach ($tgt in $targets) {
    Invoke-SSHCommand -SessionId $s.SessionId -Command "docker exec $container mkdir -p $tgt" | Out-Null
    foreach ($f in $files) {
        $src = Join-Path $pluginDir $f
        if (-not (Test-Path $src)) { continue }
        $bytes = [System.IO.File]::ReadAllBytes($src)
        $b64 = [Convert]::ToBase64String($bytes)
        $cs = 50000
        $n = [Math]::Ceiling($b64.Length / $cs)
        $tmp = "/tmp/deploy_$f.b64"
        for ($i = 0; $i -lt $n; $i++) {
            $st = $i * $cs
            $ln = [Math]::Min($cs, $b64.Length - $st)
            $ch = $b64.Substring($st, $ln)
            $op = if ($i -eq 0) { ">" } else { ">>" }
            Invoke-SSHCommand -SessionId $s.SessionId -Command "echo -n '$ch' $op $tmp" | Out-Null
        }
        $dst = "$tgt/$f"
        Invoke-SSHCommand -SessionId $s.SessionId -Command "base64 -d $tmp > /tmp/deploy_$f" | Out-Null
        $r = Invoke-SSHCommand -SessionId $s.SessionId -Command "docker cp /tmp/deploy_$f ${container}:$dst"
        if ($r.ExitStatus -eq 0) {
            Write-Host "  $f -> $tgt : OK" -ForegroundColor Cyan
        } else {
            Write-Host "  $f -> $tgt : FAIL $($r.Error)" -ForegroundColor Red
        }
    }
}

# === 3. sync package.json + package.v2.json to local repo ===
$pkgFiles = @(
    @{src="$repoRoot\package.json"; dst="/config/plugins_repo/package.json"},
    @{src="$repoRoot\package.v2.json"; dst="/config/plugins_repo/package.v2.json"}
)
foreach ($p in $pkgFiles) {
    $bytes = [System.IO.File]::ReadAllBytes($p.src)
    $b64 = [Convert]::ToBase64String($bytes)
    $tmp = "/tmp/deploy_pkg.b64"
    Invoke-SSHCommand -SessionId $s.SessionId -Command "echo -n '$b64' > $tmp" | Out-Null
    Invoke-SSHCommand -SessionId $s.SessionId -Command "base64 -d $tmp > /tmp/deploy_pkg" | Out-Null
    $r = Invoke-SSHCommand -SessionId $s.SessionId -Command "docker cp /tmp/deploy_pkg ${container}:$($p.dst)"
    if ($r.ExitStatus -eq 0) {
        Write-Host "  $(Split-Path $p.dst -Leaf) -> $($p.dst) : OK" -ForegroundColor Cyan
    } else {
        Write-Host "  $(Split-Path $p.dst -Leaf) -> $($p.dst) : FAIL" -ForegroundColor Red
    }
}

# === 4. clear __pycache__ ===
Invoke-SSHCommand -SessionId $s.SessionId -Command "docker exec $container rm -rf /app/app/plugins/dualsub/__pycache__" | Out-Null
Write-Host "__pycache__ cleared" -ForegroundColor Green

# === 5. reload API (no container restart!) ===
try {
    $reloadUrl = "$mpUrl/api/v1/plugin/reload/$pluginId" + "?apikey=$apiKey&token=$apiKey"
    $r = Invoke-RestMethod -Uri $reloadUrl -Method Get -TimeoutSec 30
    if ($r.success) {
        Write-Host "Plugin hot-reloaded (container NOT restarted)" -ForegroundColor Green
    } else {
        Write-Host "Reload returned: $($r | ConvertTo-Json -Depth 3)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "Reload API failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Manual fallback: MoviePilot UI -> plugins -> DualSub -> reload" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Deploy done." -ForegroundColor Green
