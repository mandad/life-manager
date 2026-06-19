<#
.SYNOPSIS
  Read the ship's live NMEA position/nav off the LAN and HTTPS-POST it (plaintext JSON) to the relay.
  User-space on a Windows work PC (PowerShell 5.1+, no admin, no install).

.DESCRIPTION
  Position is public-equivalent (already on Windy by call sign), so no payload encryption — the POST
  is token-gated over HTTPS so only you can write. The relay appends each push to a time-series DB
  and serves the latest to the laptop.

  Secrets from environment (set per-user with setx; do NOT hardcode):
    $env:SHIP_RELAY_URL          e.g. https://yourdomain/scs/ship-relay.php
    $env:SHIP_RELAY_PUSH_TOKEN   matches the relay's push token

.PARAMETER NmeaMode   'UDP' (unicast/broadcast), 'Multicast', or 'TCP'. Default UDP.
.PARAMETER NmeaPort   UDP/TCP port the ship broadcasts NMEA on (try 10110 first; see README discovery).
.PARAMETER NmeaHost   TCP server IP, or UDP bind address (default 0.0.0.0).
.PARAMETER MulticastGroup  Multicast mode group address (e.g. 239.x.x.x).
.PARAMETER ListenSec  Seconds to collect NMEA before sending (default 6).
.PARAMETER DryRun     Parse + print the JSON that would be POSTed; do not send. (No relay/env needed.)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File push-position.ps1 -NmeaMode UDP -NmeaPort 10110
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File push-position.ps1 -NmeaPort 10110 -DryRun
#>
[CmdletBinding()]
param(
  [ValidateSet('UDP','Multicast','TCP')] [string]$NmeaMode = 'UDP',
  [int]$NmeaPort = 0,
  [string]$NmeaHost = '0.0.0.0',
  [string]$MulticastGroup = '',
  [int]$ListenSec = 6,
  [switch]$DryRun
)

function ConvertTo-Decimal {
  param([string]$Ddmm, [string]$Hemi, [int]$DegDigits)
  if ([string]::IsNullOrWhiteSpace($Ddmm)) { return $null }
  $deg = [double]$Ddmm.Substring(0, $DegDigits)
  $min = [double]$Ddmm.Substring($DegDigits)
  $dec = $deg + ($min / 60.0)
  if ($Hemi -eq 'S' -or $Hemi -eq 'W') { $dec = -$dec }
  return [math]::Round($dec, 6)
}

function Parse-Nmea {
  param([string[]]$Lines)
  $r = @{ lat=$null; lon=$null; sog_kt=$null; cog=$null; heading=$null }
  foreach ($line in $Lines) {
    $l = $line.Trim()
    if ($l.Length -lt 6 -or $l[0] -ne '$') { continue }
    $body = ($l -split '\*')[0].Substring(1)
    $f = $body -split ','
    $type = if ($f[0].Length -ge 5) { $f[0].Substring(2) } else { $f[0] }
    switch ($type) {
      'GGA' { if ($f.Count -ge 7 -and $f[6] -ne '0' -and $f[2]) {
                $r.lat = ConvertTo-Decimal $f[2] $f[3] 2
                $r.lon = ConvertTo-Decimal $f[4] $f[5] 3 } }
      'RMC' { if ($f.Count -ge 9 -and $f[2] -eq 'A') {
                if ($null -eq $r.lat) { $r.lat = ConvertTo-Decimal $f[3] $f[4] 2; $r.lon = ConvertTo-Decimal $f[5] $f[6] 3 }
                if ($f[7]) { $r.sog_kt = [double]$f[7] }
                if ($f[8]) { $r.cog = [double]$f[8] } } }
      'VTG' { if ($f.Count -ge 6) {
                if ($f[1]) { $r.cog = [double]$f[1] }
                if ($f[5]) { $r.sog_kt = [double]$f[5] } } }
      'HDT' { if ($f.Count -ge 2 -and $f[1]) { $r.heading = [double]$f[1] } }
    }
  }
  return $r
}

function Receive-Nmea {
  param([string]$Mode, [int]$Port, [string]$Bind, [string]$Group, [int]$Seconds)
  $lines = New-Object System.Collections.Generic.List[string]
  $deadline = (Get-Date).AddSeconds($Seconds)
  if ($Mode -eq 'TCP') {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect($Bind, $Port)
    $stream = $client.GetStream(); $stream.ReadTimeout = 1000
    $reader = New-Object System.IO.StreamReader($stream)
    while ((Get-Date) -lt $deadline) {
      try { $line = $reader.ReadLine(); if ($line) { $lines.Add($line) } } catch { Start-Sleep -Milliseconds 100 }
    }
    $client.Close()
  } else {
    $udp = New-Object System.Net.Sockets.UdpClient
    $udp.Client.SetSocketOption('Socket','ReuseAddress',$true)
    $udp.Client.ReceiveTimeout = 1000
    $udp.Client.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($Bind), $Port)))
    if ($Mode -eq 'Multicast' -and $Group) { $udp.JoinMulticastGroup([System.Net.IPAddress]::Parse($Group)) }
    $remote = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
    while ((Get-Date) -lt $deadline) {
      try {
        $bytes = $udp.Receive([ref]$remote)
        $text = [System.Text.Encoding]::ASCII.GetString($bytes)
        foreach ($ln in ($text -split "`r?`n")) { if ($ln) { $lines.Add($ln) } }
      } catch { }
    }
    $udp.Close()
  }
  return $lines
}

# ---------------- main ----------------
if ($NmeaPort -le 0) { Write-Error "Specify -NmeaPort (the ship's NMEA broadcast port; try 10110)."; exit 2 }

$lines = Receive-Nmea -Mode $NmeaMode -Port $NmeaPort -Bind $NmeaHost -Group $MulticastGroup -Seconds $ListenSec
$nav = Parse-Nmea -Lines $lines
if ($null -eq $nav.lat -or $null -eq $nav.lon) {
  Write-Error "No valid GGA/RMC fix parsed from $($lines.Count) NMEA lines on $NmeaMode port $NmeaPort."
  exit 3
}

$payload = [ordered]@{
  lat = $nav.lat; lon = $nav.lon
  sog_kt = $nav.sog_kt; cog = $nav.cog; heading = $nav.heading
  utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  src = "FA-NMEA-$NmeaMode$NmeaPort"
}
$json = ($payload | ConvertTo-Json -Compress)

if ($DryRun) { Write-Host "DryRun ($($lines.Count) NMEA lines) — would POST:"; Write-Host $json; exit 0 }

$url   = $env:SHIP_RELAY_URL
$token = $env:SHIP_RELAY_PUSH_TOKEN
if (-not $url -or -not $token) { Write-Error "Set \$env:SHIP_RELAY_URL and \$env:SHIP_RELAY_PUSH_TOKEN first."; exit 2 }

try {
  Invoke-RestMethod -Uri $url -Method Post -Headers @{ 'X-Push-Token' = $token } `
    -Body $json -ContentType 'application/json' -TimeoutSec 30 | Out-Null
  Write-Host "Pushed $($nav.lat),$($nav.lon) ($($lines.Count) NMEA lines) at $($payload.utc)"
} catch {
  Write-Error "POST to relay failed: $($_.Exception.Message)"
  exit 4
}
