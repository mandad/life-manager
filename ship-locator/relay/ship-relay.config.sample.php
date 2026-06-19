<?php
/**
 * Copy to ship-relay.config.php and fill in. The .htaccess here blocks this file + the DB from being
 * served; for max safety set $DB_FILE to a path OUTSIDE your web root.
 *
 * Generate tokens (on the laptop):
 *   openssl rand -hex 24   # -> $PUSH_TOKEN  (also set $env:SHIP_RELAY_PUSH_TOKEN on the ship PC)
 *   openssl rand -hex 24   # -> $READ_TOKEN  (also set $SHIP_RELAY_READ_TOKEN on the laptop)
 */
$PUSH_TOKEN = 'PASTE_PUSH_TOKEN_HERE';
$READ_TOKEN = 'PASTE_READ_TOKEN_HERE';

// --- Storage: SQLite (default, zero-config on DreamHost via pdo_sqlite) ---
// Recommend a path OUTSIDE the web root so the DB is never directly fetchable:
$DB_FILE = dirname(__DIR__, 2) . '/ship-data/ship-positions.db';   // ensure that dir exists + is writable
// (If omitted, defaults to .ship-positions.db next to ship-relay.php — the .htaccess denies it.)

// --- OR MySQL (DreamHost panel: create DB + user), instead of $DB_FILE ---
// $DB_DSN  = 'mysql:host=mysql.yourdomain.com;dbname=shippos;charset=utf8mb4';
// $DB_USER = 'shippos_user';
// $DB_PASS = '...';
