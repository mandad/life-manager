<?php
/**
 * ship-relay.php — relay + time-series store for ship position/nav (DreamHost shared hosting).
 *
 * Position is public-equivalent (already shown on Windy by call sign), so no payload encryption —
 * just token-gated HTTPS so only you can write/read. Each push is appended to a SQLite table for
 * later query; the latest row is what /daily reads.
 *
 *   POST  (header X-Push-Token)  body = JSON {lat,lon,sog_kt?,cog?,heading?,wtmp?,atmp?,baro?,wspd?,utc?,src?}
 *         -> validates + inserts a row (received_at stamped server-side)
 *   GET   (?read=<token>  OR header X-Read-Token)
 *         (default)                 -> latest row as JSON
 *         &history=1[&since=ISO][&until=ISO][&limit=N][&format=json|csv]  -> time series
 *
 * Config (ship-relay.config.php, denied by .htaccess): $PUSH_TOKEN, $READ_TOKEN,
 *   and either $DB_FILE (sqlite path) or a full $DB_DSN (+$DB_USER/$DB_PASS) for MySQL.
 */
declare(strict_types=1);
header('Cache-Control: no-store');

$cfg = __DIR__ . '/ship-relay.config.php';
if (!is_file($cfg)) { http_response_code(500); exit('relay not configured'); }
require $cfg;

function db(): PDO {
    global $DB_DSN, $DB_USER, $DB_PASS, $DB_FILE;
    $dsn = $DB_DSN ?? ('sqlite:' . ($DB_FILE ?? (__DIR__ . '/.ship-positions.db')));
    $pdo = new PDO($dsn, $DB_USER ?? null, $DB_PASS ?? null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    $driver = $pdo->getAttribute(PDO::ATTR_DRIVER_NAME);
    $idcol = $driver === 'mysql'
        ? 'id INTEGER PRIMARY KEY AUTO_INCREMENT'
        : 'id INTEGER PRIMARY KEY AUTOINCREMENT';
    $pdo->exec("CREATE TABLE IF NOT EXISTS positions (
        $idcol, utc TEXT NOT NULL,
        lat REAL, lon REAL, sog_kt REAL, cog REAL, heading REAL,
        wtmp REAL, atmp REAL, baro REAL, wspd REAL,
        src TEXT, received_at TEXT NOT NULL )");
    $pdo->exec("CREATE INDEX IF NOT EXISTS idx_positions_utc ON positions(utc)");
    return $pdo;
}

function numornull($v) { return is_numeric($v) ? $v + 0 : null; }

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($method === 'POST') {
    $tok = $_SERVER['HTTP_X_PUSH_TOKEN'] ?? '';
    if (!is_string($tok) || !hash_equals($PUSH_TOKEN, $tok)) { http_response_code(403); exit('forbidden'); }
    $raw = (string) file_get_contents('php://input', false, null, 0, 8192);
    $d = json_decode($raw, true);
    if (!is_array($d) || !isset($d['lat'], $d['lon']) || !is_numeric($d['lat']) || !is_numeric($d['lon'])) {
        http_response_code(400); exit('bad payload (need numeric lat,lon)');
    }
    $lat = $d['lat'] + 0; $lon = $d['lon'] + 0;
    if ($lat < -90 || $lat > 90 || $lon < -180 || $lon > 180) { http_response_code(400); exit('lat/lon out of range'); }
    $utc = (isset($d['utc']) && is_string($d['utc'])) ? substr($d['utc'], 0, 32) : gmdate('Y-m-d\TH:i:s\Z');
    try {
        $pdo = db();
        $st = $pdo->prepare("INSERT INTO positions
            (utc,lat,lon,sog_kt,cog,heading,wtmp,atmp,baro,wspd,src,received_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)");
        $st->execute([$utc, $lat, $lon,
            numornull($d['sog_kt'] ?? null), numornull($d['cog'] ?? null), numornull($d['heading'] ?? null),
            numornull($d['wtmp'] ?? null), numornull($d['atmp'] ?? null), numornull($d['baro'] ?? null),
            numornull($d['wspd'] ?? null),
            isset($d['src']) ? substr((string)$d['src'], 0, 64) : null,
            gmdate('Y-m-d\TH:i:s\Z')]);
    } catch (Throwable $e) { http_response_code(500); exit('db error'); }
    echo 'ok';
    exit;
}

if ($method === 'GET') {
    $tok = $_GET['read'] ?? ($_SERVER['HTTP_X_READ_TOKEN'] ?? '');
    if (!is_string($tok) || !hash_equals($READ_TOKEN, $tok)) { http_response_code(403); exit('forbidden'); }
    try { $pdo = db(); } catch (Throwable $e) { http_response_code(500); exit('db error'); }

    if (!empty($_GET['history'])) {
        $where = []; $args = [];
        if (!empty($_GET['since']))  { $where[] = 'utc >= ?'; $args[] = substr((string)$_GET['since'], 0, 32); }
        if (!empty($_GET['until']))  { $where[] = 'utc <= ?'; $args[] = substr((string)$_GET['until'], 0, 32); }
        $limit = isset($_GET['limit']) ? max(1, min(5000, (int)$_GET['limit'])) : 500;
        $sql = "SELECT utc,lat,lon,sog_kt,cog,heading,wtmp,atmp,baro,wspd,src FROM positions"
             . ($where ? ' WHERE ' . implode(' AND ', $where) : '')
             . " ORDER BY utc DESC LIMIT $limit";
        $st = $pdo->prepare($sql); $st->execute($args);
        $rows = $st->fetchAll();
        if (($_GET['format'] ?? 'json') === 'csv') {
            header('Content-Type: text/csv');
            $cols = ['utc','lat','lon','sog_kt','cog','heading','wtmp','atmp','baro','wspd','src'];
            echo implode(',', $cols) . "\n";
            foreach ($rows as $r) { echo implode(',', array_map(fn($c) => $r[$c] ?? '', $cols)) . "\n"; }
        } else {
            header('Content-Type: application/json');
            echo json_encode(['count' => count($rows), 'positions' => $rows]);
        }
        exit;
    }

    // default: latest row
    $row = $pdo->query("SELECT utc,lat,lon,sog_kt,cog,heading,wtmp,atmp,baro,wspd,src
                        FROM positions ORDER BY utc DESC LIMIT 1")->fetch();
    if (!$row) { http_response_code(404); exit('no data yet'); }
    header('Content-Type: application/json');
    echo json_encode($row);
    exit;
}

http_response_code(405);
exit('method not allowed');
