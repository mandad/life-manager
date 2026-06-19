# ship-locator

Push NOAA Ship *Fairweather*'s live position/nav off the ship work PC (no admin) → your DreamHost
relay, which **serves the latest fix to /daily and logs a time series to a database** for later query.

```
[ship work PC, user-space]                    [DreamHost shared hosting]            [laptop]
push-position.ps1                              ship-relay.php + SQLite               scripts/ship-position.py
  read NMEA off ship LAN (UDP/TCP)               POST (X-Push-Token):                  GET latest (?read=token) → lat/lon
  parse $GPGGA/$GPRMC/$GPVTG/$HEHDT               validate + INSERT row                 → feeds nws_get_marine_forecast
  POST plaintext JSON ───────HTTPS 443────────►   GET: latest row, or                   GET history → time-series query
                                                  history (since/until/limit, json/csv)  (track, correlate w/ weather)
```

**No payload encryption** — the ship's position is public-equivalent (already on Windy by call sign),
so this just token-gates writes/reads over HTTPS so only you touch it. **No admin** (PowerShell + a
user Task Scheduler job), **outbound 443 only**. Position egress is your call as CO; policy here is
permissive, so this is a light note, not a gate.

## Setup

### 1. Two tokens (on the laptop)
```bash
openssl rand -hex 24   # PUSH token -> relay config + $env:SHIP_RELAY_PUSH_TOKEN (ship)
openssl rand -hex 24   # READ token -> relay config + $SHIP_RELAY_READ_TOKEN (laptop)
```

### 2. Deploy the relay (DreamHost)
- Upload `relay/ship-relay.php` + `relay/.htaccess` to a folder under your domain, e.g. `…/scs/`.
- Copy `ship-relay.config.sample.php` → `ship-relay.config.php`; paste the two tokens.
- Set `$DB_FILE` to a path **outside** the web root (sample shows `…/ship-data/ship-positions.db`) and
  make that dir writable. SQLite needs no setup — DreamHost ships `pdo_sqlite`. (MySQL alt in the sample.)
- The table is auto-created on first POST. Relay URL = `https://YOURDOMAIN/scs/ship-relay.php`.

### 3. Laptop config (`~/.bashrc`)
```bash
export SHIP_RELAY_URL="https://YOURDOMAIN/scs/ship-relay.php"
export SHIP_RELAY_READ_TOKEN="<read token>"
```
(/daily runs the puller via `bash -ic` so these load. No extra Python deps — stdlib only now.)

### 4. Ship work PC (no admin)
```powershell
setx SHIP_RELAY_URL        "https://YOURDOMAIN/scs/ship-relay.php"
setx SHIP_RELAY_PUSH_TOKEN "<push token>"
```
(Re-open PowerShell after `setx`.) **Find the NMEA port** — try **10110** (IANA NMEA-0183-over-IP) first; ask the SCS/survey techs, or sniff (no admin):
```powershell
foreach ($p in 10110,5005,5006,5007,2000,4001,3000) {
  try { $u=New-Object System.Net.Sockets.UdpClient; $u.Client.ReceiveTimeout=2500
        $u.Client.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any,$p)))
        $ep=New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any,0)
        $t=[System.Text.Encoding]::ASCII.GetString($u.Receive([ref]$ep)); $u.Close()
        if ($t -match '\$..(GGA|RMC|VTG)') { "PORT $p : $($t.Substring(0,[Math]::Min(60,$t.Length)))" } } catch { $u.Close() }
}
```
Multicast (239.x.x.x) or TCP-to-a-server? Use `-NmeaMode Multicast -MulticastGroup …` or `-NmeaMode TCP -NmeaHost …`.

Dry-run (no relay/env needed — just parses + prints the JSON):
```powershell
powershell -ExecutionPolicy Bypass -File push-position.ps1 -NmeaPort 10110 -DryRun
```
Then schedule (user task, no admin) every 30 min:
```powershell
schtasks /Create /SC MINUTE /MO 30 /TN "ShipPositionPush" ^
  /TR "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\path\to\push-position.ps1 -NmeaMode UDP -NmeaPort 10110"
```

### 5. Verify
- Ship: real run → `Pushed <lat>,<lon> (N NMEA lines) at <utc>`.
- Laptop: `bash -ic 'python3 scripts/ship-position.py'` → shows the fix + freshness.
- DB accumulating: `bash -ic 'python3 scripts/ship-position.py --history --limit 10'`.

## Pull / query (laptop)
```bash
bash -ic 'python3 scripts/ship-position.py'                 # latest fix + age + the forecast call to make
bash -ic 'python3 scripts/ship-position.py --latlon'        # "LAT LON" → pipe to nws_get_marine_forecast
bash -ic 'python3 scripts/ship-position.py --json'
bash -ic 'python3 scripts/ship-position.py --history --since 2026-06-01T00:00:00Z --limit 500'   # track summary
bash -ic 'python3 scripts/ship-position.py --history --csv > track.csv'                          # full export
```
The server DB persists independently of the laptop — query any window later (reconstruct a transit
track, correlate the ship's path against the weather/obs in `_daily-data/`). At ~30-min pushes the DB
grows ~17k rows/yr (trivial); prune with a `DELETE FROM positions WHERE utc < …` if ever needed.

/daily Step 7.5 uses the **latest** fix when fresh (≤3h; puller flags `⚠️STALE` otherwise) for the
marine-forecast point + `synoptic-obs.py`; falls back to the sailing-schedule leg when stale/undeployed.

## Troubleshooting
- **No fix parsed:** wrong port/mode — re-run the sniff or ask SCS techs; some ships emit only RMC (handled) or multicast/TCP.
- **POST 403 / GET 403:** push or read token mismatch (ship env / laptop env vs relay config).
- **DB error / 500:** confirm `pdo_sqlite` enabled + `$DB_FILE` dir exists & is writable (or use the MySQL DSN).
- **STALE in /daily:** pusher down or ship off-network → falls back to the schedule leg automatically.
