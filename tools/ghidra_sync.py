#!/usr/bin/env python3
"""
ghidra_sync.py — Bidirectional Ghidra ↔ symbol_hints.json sync for ACCF.

Queries the running GhidraMCP HTTP server (localhost:8080/18001/9090/8765)
for all named functions and merges any real names (not FUN_/DAT_/LAB_ auto-
names) into data/symbol_hints.json at high confidence (0.9).

Also optionally pushes names back TO Ghidra: any hint with confidence ≥ 0.8
whose address maps to a FUN_XXXXXXXX in Ghidra gets renamed there.

Usage:
  python3 tools/ghidra_sync.py             # pull from Ghidra → hints
  python3 tools/ghidra_sync.py --push      # also push hints → Ghidra
  python3 tools/ghidra_sync.py --report    # show current hints, no changes
  python3 tools/ghidra_sync.py --port 8080 # force a specific port

If Ghidra is not running the script exits cleanly (no error) so autopilot
can call it unconditionally.
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYMBOLS_FILE = PROJECT_ROOT / "config" / "RUUE01" / "symbols.txt"
HINTS_FILE   = PROJECT_ROOT / "data" / "symbol_hints.json"

# Auto-generated Ghidra name prefixes — these are NOT real names
_SKIP_PREFIXES = (
    "FUN_", "DAT_", "LAB_", "BYTE_", "WORD_", "DWORD_", "FLOAT_",
    "PTR_", "SUB_", "UNK_", "SWITCH_", "caseD_", "s_", "u_",
    "thunk_FUN_", "thunk_DAT_",  # auto-generated thunk wrappers
    "fn_",  # already in our system
)


# ─── GhidraMCP REST client ────────────────────────────────────────────────────

def _find_server(force_port: int | None = None) -> str | None:
    """Return the base URL of the running GhidraMCP HTTP server, or None."""
    try:
        import httpx
    except ImportError:
        print("  ⚠  httpx not installed — pip install httpx")
        return None

    ports = [force_port] if force_port else [8080, 18001, 9090, 8765]
    with httpx.Client() as client:
        for port in ports:
            url = f"http://localhost:{port}"
            for probe in ("/ping", "/"):
                try:
                    r = client.get(f"{url}{probe}", timeout=1.5)
                    if r.status_code < 500:
                        return url
                except Exception:
                    continue
    return None


def _ghidra_get(base: str, endpoint: str, **kwargs) -> dict | list | None:
    """GET from GhidraMCP. Returns parsed JSON or None."""
    try:
        import httpx
        r = httpx.get(f"{base}{endpoint}", timeout=15.0, **kwargs)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _ghidra_post(base: str, endpoint: str, payload: dict) -> dict | None:
    """POST to GhidraMCP. Returns parsed JSON or None."""
    try:
        import httpx
        r = httpx.post(f"{base}{endpoint}", json=payload, timeout=15.0)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def list_named_functions(base: str) -> list[dict]:
    """
    Ask GhidraMCP for all functions.  Try several endpoint styles since
    different GhidraMCP versions expose different paths.
    Returns list of {"name": str, "address": str}.
    """
    result = None

    # Style 1 — GET /list_functions
    result = _ghidra_get(base, "/list_functions")
    if isinstance(result, list):
        return result

    # Style 2 — GET /functions
    result = _ghidra_get(base, "/functions")
    if isinstance(result, list):
        return result

    # Style 3 — POST /functions/list
    result = _ghidra_post(base, "/functions/list", {})
    if result and "functions" in result:
        return result["functions"]

    # Style 4 — MCP JSON-RPC call_tool
    rpc = _ghidra_post(base, "/", {
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "list_functions", "arguments": {}},
    })
    if rpc:
        content = rpc.get("result", {})
        if isinstance(content, list):
            return content
        if isinstance(content, dict) and "functions" in content:
            return content["functions"]

    return []


def rename_function(base: str, address: str, new_name: str) -> bool:
    """Tell Ghidra to rename the function at address to new_name."""
    # Try several rename endpoint styles
    for ep, payload in [
        ("/rename_function_by_address",
         {"function_address": address, "new_name": new_name}),
        ("/rename_function",
         {"address": address, "name": new_name}),
        ("/functions/rename",
         {"address": address, "new_name": new_name}),
    ]:
        r = _ghidra_post(base, ep, payload)
        if r is not None:
            return True
    return False


# ─── symbols.txt helpers ──────────────────────────────────────────────────────

def load_symbols_map() -> dict[str, str]:
    """Return {hex_addr_upper → symbol_name} from symbols.txt."""
    mapping: dict[str, str] = {}
    if not SYMBOLS_FILE.exists():
        return mapping
    for line in SYMBOLS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"^(\S+)\s*=\s*\.\w+:0x([0-9A-Fa-f]+)", line)
        if m:
            mapping[m.group(2).upper()] = m.group(1)
    return mapping


# ─── Hints helpers ────────────────────────────────────────────────────────────

def load_hints() -> dict:
    if HINTS_FILE.exists():
        with open(HINTS_FILE) as f:
            return json.load(f)
    return {}


def save_hints(hints: dict):
    HINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HINTS_FILE, "w") as f:
        json.dump(hints, f, indent=2, sort_keys=True)


def _is_real_name(name: str) -> bool:
    """Return True if the name looks human-assigned rather than auto-generated."""
    if not name or len(name) < 2:
        return False
    if name.startswith(_SKIP_PREFIXES):
        return False
    # Skip pure hex addresses
    if re.match(r'^[0-9A-Fa-f]+$', name):
        return False
    return True


# ─── Pull: Ghidra → hints ─────────────────────────────────────────────────────

def pull_from_ghidra(base: str, hints: dict) -> int:
    """
    Fetch all named functions from Ghidra. For each function with a real
    name (not FUN_/DAT_ etc.) merge it into hints at confidence 0.9.
    Returns number of new/updated hints.
    """
    funcs = list_named_functions(base)
    if not funcs:
        print("  ⚠  Ghidra returned no functions (endpoint may differ)")
        return 0

    print(f"  📥  Ghidra returned {len(funcs)} function entries")

    # Build address→fn_key map from symbols.txt so we can correlate addresses
    sym_map = load_symbols_map()  # ADDR_HEX_UPPER → our fn_name

    added = 0
    for entry in funcs:
        name    = str(entry.get("name", ""))
        raw_addr = str(entry.get("address", entry.get("start", "")))

        # Normalise address
        addr = raw_addr.upper().lstrip("0X").lstrip("0") or "0"
        addr = addr.zfill(8)  # pad to 8 chars for consistent key

        if not _is_real_name(name):
            continue

        # Build our hint key: fn_XXXXXXXX
        key = f"fn_{addr}"

        existing = hints.get(key, {})
        if existing.get("confidence", 0) >= 0.9 and existing.get("name") == name:
            continue  # already have this

        hints[key] = {
            "name":       name,
            "source":     "ghidra",
            "confidence": 0.9,
            "address":    addr,
        }
        added += 1

    return added


# ─── Push: hints → Ghidra ─────────────────────────────────────────────────────

def push_to_ghidra(base: str, hints: dict, min_confidence: float = 0.8) -> int:
    """
    For each high-confidence hint, rename the function in Ghidra if it
    currently has an auto-generated name (FUN_XXXXXXXX etc.).
    Returns number of renames attempted.
    """
    pushed = 0
    for key, info in hints.items():
        addr_m = re.search(r"fn_([0-9A-Fa-f]+)", key, re.IGNORECASE)
        if not addr_m:
            continue
        if info.get("confidence", 0) < min_confidence:
            continue
        name = info.get("name", "")
        if not _is_real_name(name):
            continue

        addr_hex = addr_m.group(1).upper()

        # Check what Ghidra currently calls this function
        existing = _ghidra_post(base, "/get_function_by_address",
                                 {"address": addr_hex})
        if existing is None:
            existing = _ghidra_get(base, f"/function/{addr_hex}")

        if isinstance(existing, dict):
            current_name = existing.get("name", "")
            if current_name and not current_name.startswith(_SKIP_PREFIXES):
                continue  # Ghidra already has a real name here — don't overwrite
            # Sanitise name for Ghidra (no spaces, special chars)
            safe = re.sub(r'[^A-Za-z0-9_]', '_', name)
            if rename_function(base, addr_hex, safe):
                pushed += 1
                print(f"    ↑  fn_{addr_hex} → {safe}")

    return pushed


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(hints: dict):
    ghidra_hints = {k: v for k, v in hints.items() if v.get("source") == "ghidra"}
    by_conf = sorted(hints.items(), key=lambda kv: -kv[1].get("confidence", 0))
    print(f"\n  Symbol hint summary ({len(hints)} total, {len(ghidra_hints)} from Ghidra):")
    print(f"  {'KEY':<22}  {'CONF':>4}  {'SOURCE':<14}  NAME")
    print(f"  {'-'*22}  {'-'*4}  {'-'*14}  {'-'*30}")
    for key, info in by_conf[:60]:
        conf   = info.get("confidence", 0)
        source = info.get("source", "?")
        name   = info.get("name", "?")[:40]
        print(f"  {key:<22}  {conf:.2f}  {source:<14}  {name}")
    if len(hints) > 60:
        print(f"  … ({len(hints) - 60} more)")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync Ghidra function names ↔ symbol_hints.json"
    )
    parser.add_argument("--push",   action="store_true",
                        help="Also push high-confidence hints back to Ghidra")
    parser.add_argument("--report", action="store_true",
                        help="Print current hints and exit (no Ghidra needed)")
    parser.add_argument("--port",   type=int, default=None,
                        help="Force a specific GhidraMCP port (default: auto-detect)")
    parser.add_argument("--min-push-confidence", type=float, default=0.8,
                        help="Minimum confidence to push back to Ghidra (default 0.8)")
    parser.add_argument("--require", action="store_true",
                        help="Exit with error code 1 if Ghidra is not reachable (instead of silently skipping)")
    args = parser.parse_args()

    hints = load_hints()

    if args.report:
        print_report(hints)
        return

    base = _find_server(args.port)
    if not base:
        if args.require:
            print("  ✖  Ghidra MCP server not reachable — REQUIRED but not running!", file=sys.stderr)
            print("     Start Ghidra with the GhidraMCP plugin active and try again.", file=sys.stderr)
            sys.exit(1)
        print("  ℹ  Ghidra MCP server not reachable — skipping sync")
        print("     (start Ghidra with GhidraMCP plugin, or run analyzeHeadless)")
        if hints:
            total = len(hints)
            high  = sum(1 for v in hints.values() if v.get("confidence", 0) >= 0.8)
            print(f"     {total} hints cached ({high} high-confidence)")
        return

    print(f"  🧠  Connected to GhidraMCP at {base}")

    # Purge any previously-saved hints with auto-generated names (e.g. thunk_FUN_*)
    before = len(hints)
    hints = {k: v for k, v in hints.items() if _is_real_name(v.get("name", ""))}
    purged = before - len(hints)
    if purged:
        print(f"  🧹  Purged {purged} auto-generated hints (thunks etc.)")

    # Pull: Ghidra → hints
    added = pull_from_ghidra(base, hints)
    print(f"  +{added} new/updated hints from Ghidra")

    save_hints(hints)
    print(f"  💾  Saved {len(hints)} total hints → {HINTS_FILE.relative_to(PROJECT_ROOT)}")

    # Push: hints → Ghidra (optional)
    if args.push:
        print("\n  ↑  Pushing high-confidence hints to Ghidra …")
        pushed = push_to_ghidra(base, hints, args.min_push_confidence)
        print(f"  ↑  Renamed {pushed} function(s) in Ghidra")

    print_report(hints)


if __name__ == "__main__":
    main()
