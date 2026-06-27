#!/usr/bin/env python3
"""
mcp-sync.py - generate Claude Code (WSL) and Claude Desktop (Windows) MCP
config from one manifest, in WSL-bridge mode.

Both clients end up running the SAME Linux build of each server:
  - Claude Code (WSL .claude.json):   command = bash  -c '<inner>'
  - Claude Desktop (Windows):         command = wsl.exe -d <distro> -- bash -c '<inner>'
where <inner> is identical: source the secrets env file, then exec the
absolute WSL node against the server's dist/index.js. One build, one
node_modules, one place for secrets. No login shell -> no stdout noise to
corrupt the MCP stdio stream.

Run from WSL (so it can see ~/.claude.json AND /mnt/c/... at once):
    python3 scripts/mcp-sync.py            # apply
    python3 scripts/mcp-sync.py --dry-run  # show changes, write nothing

Runbook: AI Scratchpad/Notes/MCP config sync.md
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


def log(msg):
    print(msg)


def warn(msg):
    print(f"  [warn] {msg}", file=sys.stderr)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def find_repo_root(script_path: Path) -> Path:
    # script lives at <repo>/scripts/mcp-sync.py
    return script_path.resolve().parent.parent


def derive_win_user(repo_root: Path):
    # Expect a WSL path like /mnt/c/Users/<user>/...
    parts = repo_root.parts
    try:
        i = parts.index("Users")
        return parts[i + 1]
    except (ValueError, IndexError):
        return None


def build_inner_cmd(node_path: str, entry_abs: str, secrets_env_literal: str) -> str:
    # secrets_env_literal keeps $HOME so it resolves at runtime for whoever runs it.
    return (
        f'set -a; [ -f "{secrets_env_literal}" ] && . "{secrets_env_literal}"; set +a; '
        f'exec "{node_path}" "{entry_abs}"'
    )


def load_json(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def backup(path: Path):
    if path.exists():
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(path.name + f".bak.{ts}")
        shutil.copy2(path, bak)
        return bak
    return None


def atomic_write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Sync LLM-Land MCP config to WSL + Desktop (bridge mode).")
    ap.add_argument("--manifest", help="path to mcp-manifest.json (default: <repo>/mcp-servers/mcp-manifest.json)")
    ap.add_argument("--node", help="absolute path to WSL node (default: `which node`)")
    ap.add_argument("--claude-json", help="override WSL ~/.claude.json path")
    ap.add_argument("--desktop-config-dir", action="append", default=[],
                    help="override/add a Desktop config dir (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    ap.add_argument("--wsl-only", action="store_true", help="only update the WSL .claude.json")
    ap.add_argument("--desktop-only", action="store_true", help="only update the Desktop config")
    ap.add_argument("--no-prune-local", action="store_true",
                    help="don't remove duplicate local/project-scope defs of managed servers from .claude.json")
    args = ap.parse_args()

    script_path = Path(__file__)
    repo_root = find_repo_root(script_path)

    manifest_path = Path(args.manifest) if args.manifest else repo_root / "mcp-servers" / "mcp-manifest.json"
    manifest = load_json(manifest_path)
    if manifest is None:
        die(f"manifest not found: {manifest_path}")

    servers_root = Path(manifest["serversRootWsl"]) if manifest.get("serversRootWsl") else repo_root / "mcp-servers"
    distro = manifest.get("wslDistro", "Ubuntu")
    secrets_env_literal = manifest.get("secretsEnvPath", "$HOME/.config/llm-land-mcp/secrets.env")
    real_secrets = Path(os.path.expanduser(os.path.expandvars(secrets_env_literal)))

    node_path = args.node or shutil.which("node")
    if not node_path:
        die("could not find 'node' on PATH; run this from your normal WSL shell or pass --node /abs/path/to/node")
    node_path = str(Path(node_path))  # normalize

    servers = manifest.get("servers", {})
    if not servers:
        die("manifest has no servers")

    log(f"manifest      : {manifest_path}")
    log(f"servers root  : {servers_root}")
    log(f"wsl node      : {node_path}")
    log(f"wsl distro    : {distro}")
    log(f"secrets env   : {secrets_env_literal} -> {real_secrets} ({'found' if real_secrets.exists() else 'MISSING'})")
    log("")

    wsl_servers, desktop_servers = {}, {}
    for name, cfg in servers.items():
        entry_abs = str(servers_root / cfg["entry"])
        inner = build_inner_cmd(node_path, entry_abs, secrets_env_literal)
        wsl_servers[name] = {"command": "bash", "args": ["-c", inner]}
        desktop_servers[name] = {"command": "wsl.exe", "args": ["-d", distro, "--", "bash", "-c", inner]}
        if not Path(entry_abs).exists():
            warn(f"{name}: build output not found at {entry_abs} (run `npm install && npm run build` in WSL for this server)")

    # Validate secrets file has the keys the servers need.
    required = sorted({k for c in servers.values() for k in c.get("env", [])})
    if real_secrets.exists():
        present = set()
        for line in real_secrets.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            present.add(line.split("=", 1)[0].strip().lstrip("export ").strip())
        missing = [k for k in required if k not in present]
        if missing:
            warn(f"secrets env is missing keys: {', '.join(missing)}")
    else:
        warn(f"secrets env not found ({real_secrets}); servers will launch without their env vars")
        warn("create it: mkdir -p ~/.config/llm-land-mcp && cp mcp-servers/secrets.env.example ~/.config/llm-land-mcp/secrets.env && chmod 600 ~/.config/llm-land-mcp/secrets.env")
    log("")

    # ---- WSL: merge into ~/.claude.json (preserve everything else) ----
    if not args.desktop_only:
        cj = Path(args.claude_json) if args.claude_json else Path(os.path.expanduser("~/.claude.json"))
        data = load_json(cj) or {}
        data.setdefault("mcpServers", {})
        for name, entry in wsl_servers.items():
            data["mcpServers"][name] = entry

        # Remove duplicate local/project-scope definitions of managed servers.
        # Claude Code stores local scope under projects.<dir>.mcpServers; a server
        # defined there AND in user scope (top-level mcpServers) triggers a
        # "Conflicting scopes" warning. User scope is canonical for this setup.
        pruned = []
        if not args.no_prune_local:
            for proj_path, proj in (data.get("projects") or {}).items():
                psrv = proj.get("mcpServers") if isinstance(proj, dict) else None
                if not isinstance(psrv, dict):
                    continue
                for name in list(psrv):
                    if name in wsl_servers:
                        psrv.pop(name)
                        pruned.append((proj_path, name))

        if args.dry_run:
            log(f"[dry-run] {cj}: would set user-scope mcpServers[{', '.join(wsl_servers)}]")
            for proj_path, name in pruned:
                log(f"[dry-run] {cj}: would remove local-scope '{name}' from project {proj_path}")
        else:
            b = backup(cj)
            atomic_write_json(cj, data)
            log(f"updated {cj}" + (f"  (backup: {b.name})" if b else "  (created)"))
            for proj_path, name in pruned:
                log(f"  pruned local-scope '{name}' from project {proj_path}")

    # ---- Desktop: write claude_desktop_config.json in every live location ----
    if not args.wsl_only:
        dirs = [Path(d) for d in args.desktop_config_dir]
        if not dirs:
            win_user = manifest.get("winUser") or derive_win_user(repo_root)
            pkg = manifest.get("desktopPackageFamily", "Claude_pzs8sxrjxfjjc")
            if not win_user:
                warn("could not derive Windows user from path; pass --desktop-config-dir")
            else:
                candidates = [
                    Path(f"/mnt/c/Users/{win_user}/AppData/Local/Packages/{pkg}/LocalCache/Roaming/Claude"),
                    Path(f"/mnt/c/Users/{win_user}/AppData/Roaming/Claude"),
                ]
                dirs = [d for d in candidates if d.exists()]
                if not dirs:
                    warn("no existing Claude Desktop config dir found; looked in:")
                    for c in candidates:
                        warn(f"    {c}")
                    warn("if Desktop is installed elsewhere, pass --desktop-config-dir <wsl path to the Claude folder>")
        for d in dirs:
            cfgp = d / "claude_desktop_config.json"
            data = load_json(cfgp) or {}
            data.setdefault("mcpServers", {})
            for name, entry in desktop_servers.items():
                data["mcpServers"][name] = entry
            if args.dry_run:
                log(f"[dry-run] {cfgp}: would set mcpServers[{', '.join(desktop_servers)}]")
            else:
                b = backup(cfgp)
                atomic_write_json(cfgp, data)
                log(f"updated {cfgp}" + (f"  (backup: {b.name})" if b else "  (created)"))

        # Secret-free reference snapshot kept in the repo (survives Desktop folder resets).
        snap = servers_root / "generated" / "claude_desktop_config.generated.json"
        if args.dry_run:
            log(f"[dry-run] would write reference snapshot {snap}")
        else:
            atomic_write_json(snap, {"mcpServers": desktop_servers})
            log(f"wrote reference snapshot {snap}")

    log("")
    log("Done. Restart Claude Code and Claude Desktop to pick up changes.")


if __name__ == "__main__":
    main()
