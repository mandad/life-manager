# ship-locator

Push NOAA Ship *Fairweather*'s live position/nav off the ship work PC (no admin) → **Plotroom**,
which stores it as the ship's live position and keeps the recent fixes on the vessel's track.

```
[ship work PC, user-space]                    [Plotroom]
push-position.ps1                              POST /positions/push
  read NMEA off ship LAN (UDP/TCP)               X-Push-Token → the ship (hashed lookup)
  parse GGA/RMC/VTG + HDT/THS/HDG/HDM heading    newer fix wins → live Ship positions marker
  POST plaintext JSON ───────HTTPS 443────────►  + a track point, trimmed at 24 h and
                                                   wherever the nightly SAMOS import reaches
```

**No payload encryption** — the ship's position is public-equivalent (already on Windy by call sign),
so this just token-gates writes over HTTPS so only you touch it. **No admin** (PowerShell + a
user Task Scheduler job), **outbound 443 only**. Position egress is your call as CO; policy here is
permissive, so this is a light note, not a gate.

The older DreamHost relay (`relay/ship-relay.php` + `scripts/ship-position.py`) is still in this
repo and still described at the end — it is no longer the target. The script POSTs to exactly one
URL, so pointing it at Plotroom means the relay stops receiving new fixes.

## Setup

### 1. Get the ship's push token (in Plotroom)
Open **Ship configuration → Vessel → Position push** as the ship's CO, a NAV-grade officer (NAV,
OPS, XO), or an administrator, and press **Generate**. The token and the address to post to appear
together, once. Copy both now — the token is never shown again, and pressing **Generate** later
replaces it and kills the old one. **Revoke** turns the direct feed off.

Post to production only:

```
https://plotroom.mandabot.com/positions/push
```

The dev instance sits behind IAP (Cloud Run's own, so its `run.app` URL is covered too): a push
carrying only the token gets a sign-in redirect, never a stored fix. Dev testing is done from the
laptop with a service-account JWT (see Plotroom's docs/operations/SHIP_RELAY.md); the ship task
never targets dev.

### 2. Ship work PC (no admin)
```powershell
setx SHIP_RELAY_URL        "https://plotroom.mandabot.com/positions/push"
setx SHIP_RELAY_PUSH_TOKEN "<the token from Ship configuration>"
```
(Re-open PowerShell after `setx`.) The variable names are unchanged, and so are the JSON body and
the `X-Push-Token` header the script already sends — Plotroom accepts exactly that shape, so this
is an environment change, not a script change.

**Find the NMEA port** — try **10110** (IANA NMEA-0183-over-IP) first; ask the SCS/survey techs, or sniff (no admin):
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

Dry-run (no endpoint/env needed — just parses + prints the JSON):
```powershell
powershell -ExecutionPolicy Bypass -File push-position.ps1 -NmeaPort 10110 -DryRun
```

### 3. Schedule it (user task, no admin) every 1–5 min
Direct pushes exist to make the marker minutes old rather than hours, so run the task far more
often than the old 30-minute relay cadence. Plotroom refuses more than one push per ship every 5
seconds, and a 1–5 minute task is nowhere near that:
```powershell
schtasks /Create /SC MINUTE /MO 5 /TN "ShipPositionPush" ^
  /TR "wscript.exe \"C:\path\to\run-hidden.vbs\" -NmeaMode UDP -NmeaPort 10110"
```
`run-hidden.vbs` (next to the script) starts PowerShell with no window and passes the arguments
through. Pointing the task straight at `powershell -WindowStyle Hidden` flashes a console window
on every run: a task created by a standard user runs only in the logged-on desktop, and
powershell.exe opens its window before it reads that switch. The other no-admin fix is the task's
"Run whether user is logged on or not" setting, which some domain policies refuse.

Already have the old task (30-minute cadence, or the visible-window action)? Delete and recreate it:
```powershell
schtasks /Delete /TN "ShipPositionPush" /F
```

### 4. Verify
- Ship: real run → `Pushed <lat>,<lon> (N NMEA lines) at <utc>`.
- Plotroom: turn on **Ship positions**, click the ship's marker — the popup should show the pushed
  position with a source of **ship relay** and an age of a minute or two.
- Track: **Library → Resources → Tracks**, load the ship, set **Display range** to **Day** — the
  recent pushed fixes are on the line, marked *ship push*.

## What Plotroom does with a push
- Stores the fix as the ship's live position **only if its `utc` is newer** than what it already
  holds, so a pushed fix and the public AIS feed never clobber each other — the newer one wins.
- Appends a track point unless one is already within 30 s of it, so a retry costs nothing.
- Trims those pushed points at 24 hours, and drops any that the nightly SAMOS import has since
  covered at 1-minute resolution. They are a stopgap between imports, not a second history.
- Answers `{"ship_id": …, "stored": true|false, "track_points": …}`. `stored: false` means the push
  was recorded on the track but was older than the live fix already held.

## Troubleshooting
- **0 NMEA lines (UDP):** nothing reached the socket. Re-run with `-Verbose`: no datagrams means the Windows Firewall has no inbound rule for `powershell.exe` (a nav program installed with admin has one), or the feed is unicast and that program bound the port first (close it and retry). Workaround with no admin: connect to a TCP NMEA repeater instead (`-NmeaMode TCP -NmeaHost <ip> -NmeaPort <port>`), since outbound connections are not firewalled.
- **Heading null:** the feed carries no HDT/THS/HDG/HDM sentence (an `$ECRMC` repeater gives position, SOG and COG only). Enable heading output on the repeater's port, or listen to the raw gyro/GPS broadcast.
- **No fix parsed:** wrong port/mode — re-run the sniff or ask SCS techs; some ships emit only RMC (handled) or multicast/TCP.
- **POST 401:** the token does not match any ship — it was rotated or revoked in Ship configuration. Generate a fresh one and `setx` it again.
- **POST 429:** with a valid token this only ever means pushes are coming faster than one accepted push per 5 seconds (the rate limiter that repeated bad tokens trip never holds up a valid one). Honour the `Retry-After` header.
- **POST 422:** the parsed fix is out of range (including a non-finite `sog_kt`, which a bad VTG parse can produce), or the `utc` is more than 5 minutes in the future — check the PC's clock. A 422 does not cost the next push its 5-second slot.
- **Nothing at all (or a 302):** the ship is off the network, or the URL points at the dev instance, which IAP blocks.

## Legacy: the DreamHost relay
`relay/ship-relay.php` + `relay/.htaccess` under the domain, configured by `ship-relay.config.php`
(two tokens and a SQLite file outside the web root). It took the same POST, kept a time series, and
served the latest fix to `scripts/ship-position.py` on the laptop for the `/daily` briefing. It
still works and still holds whatever it already collected:
```bash
bash -ic 'python3 scripts/ship-position.py'                 # latest fix + age
bash -ic 'python3 scripts/ship-position.py --history --since 2026-06-01T00:00:00Z --limit 500'
bash -ic 'python3 scripts/ship-position.py --history --csv > track.csv'
```
Its old setup steps: `openssl rand -hex 24` for each of the push and read tokens, paste both into
`ship-relay.config.php`, point `$DB_FILE` outside the web root (DreamHost ships `pdo_sqlite`; the
table is auto-created on first POST), then set `SHIP_RELAY_URL` to
`https://YOURDOMAIN/scs/ship-relay.php` on the ship and `SHIP_RELAY_READ_TOKEN` on the laptop. A
403 either way is a token mismatch; a 500 is the SQLite path or permissions. Plotroom can also poll
this relay (`PLOTROOM_SHIP_RELAY_*`), but no Plotroom instance is configured to.
