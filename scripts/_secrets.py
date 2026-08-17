"""Shared secrets loader for the LLM-Land scripts (automation #42).

WHY THIS EXISTS
    Before this, secrets lived in THREE places and only one of them was documented:
      1. `~/.bashrc`                    -> SYNOPTIC_TOKEN (one value, shell-only)
      2. `scripts/.tomorrow_token`      -> TOMORROW_API_KEY, as a file INSIDE OneDrive
      3. `~/.config/llm-land-mcp/secrets.env` -> everything the MCP servers use
    The MCP servers converged on (3) long ago; the scripts never did. That split cost real
    things: a secret sitting in cloud-synced storage, a documented mechanism that was simply
    wrong (TOMORROW_API_KEY was never in .bashrc), and a `bash -ic` wrapper applied to six
    scripts when only two of them ever read a token.

    This module makes `secrets.env` the single store for scripts too, so there is one place to
    look and one place to rotate.

BEHAVIOUR
    `load_secrets()` parses the env file and sets only keys that are **not already present** in
    os.environ. Anything the caller already exported always wins, so:
      - running under `bash -ic` (the old way) still works and is unaffected;
      - CI / one-off overrides (`SYNOPTIC_TOKEN=... python3 scripts/foo.py`) still win;
      - and a plain `python3 scripts/foo.py` now works with no wrapper at all.

    Mirrors `loadSecretsEnv()` in the TypeScript servers (`onenote/src/auth.ts`,
    `google-tasks/src/auth.ts`) deliberately — same file, same precedence rule — so there is
    one convention across both halves of the system rather than two.

    Never raises and never prints: a missing or unreadable file is a normal state (a fresh
    clone has no secrets), and callers already emit their own actionable "no token" messages.
    Values are never logged.

FORMAT
    Shell-ish `KEY=value`, matching what `mcp-sync.py` sources:
      - `export ` prefix tolerated
      - `#` comments and blank lines skipped
      - surrounding single/double quotes stripped
      - `$HOME` / `${HOME}` / `${VAR}` expanded, as bash would when sourcing
"""

import os
import re
from pathlib import Path

DEFAULT_PATH = Path.home() / ".config" / "llm-land-mcp" / "secrets.env"

_VAR_RE = re.compile(r"\$\{?(\w+)\}?")


def secrets_path() -> Path:
    """Env file location. `LLM_LAND_SECRETS_ENV` overrides, matching the TS servers."""
    override = os.environ.get("LLM_LAND_SECRETS_ENV")
    return Path(override) if override else DEFAULT_PATH


def load_secrets(path=None) -> int:
    """Load KEY=value pairs into os.environ without overriding what's already set.

    Returns the number of keys added (0 when the file is absent, unreadable, or every key was
    already present). Deliberately silent — see module docstring."""
    p = Path(path) if path else secrets_path()
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    added = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not key or key in os.environ:      # already-set env always wins
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        val = _VAR_RE.sub(
            lambda m: str(Path.home()) if m.group(1) == "HOME" else os.environ.get(m.group(1), ""),
            val,
        )
        os.environ[key] = val
        added += 1
    return added
