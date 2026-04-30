#!/usr/bin/env python3
"""
cross_game_symbols.py — Cross-game symbol analysis for ACCF decompilation.

Sources:
  • data/nl_symbols.json   — RTTI class names from Animal Crossing: New Leaf (3DS)
  • data/bbq_symbols.json  — CodeWarrior-mangled symbols from Amiibo Festival (Wii U)

What this script does:
  1. Scans config/RUUE01/symbols.txt for CW-mangled names containing known AC class
     names, then emits symbol_hints.json entries for matched fn_* placeholders.
  2. Builds data/ac_class_context.txt — a compact list of known AC class names
     injected into the LLM context by decomp_loop.py when relevant.
  3. Reports how many fn_* symbols have potential class matches.

Usage:
  python3 tools/cross_game_symbols.py           # analyse + update hints
  python3 tools/cross_game_symbols.py --dry-run # report without writing
  python3 tools/cross_game_symbols.py --context # (re)build ac_class_context.txt only
"""

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT   = Path(__file__).resolve().parent.parent
SYMBOLS_FILE   = PROJECT_ROOT / "config" / "RUUE01" / "symbols.txt"
HINTS_FILE     = PROJECT_ROOT / "data" / "symbol_hints.json"
NL_SYMS_FILE   = PROJECT_ROOT / "data" / "nl_symbols.json"
BBQ_SYMS_FILE  = PROJECT_ROOT / "data" / "bbq_symbols.json"
CONTEXT_FILE   = PROJECT_ROOT / "data" / "ac_class_context.txt"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict | list:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_hints(hints: dict):
    HINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HINTS_FILE, "w") as f:
        json.dump(hints, f, indent=2, sort_keys=True)


def load_symbols() -> dict[str, str]:
    """Return {symbol_name: address_hex} from symbols.txt."""
    syms: dict[str, str] = {}
    if not SYMBOLS_FILE.exists():
        return syms
    for line in SYMBOLS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"^(\S+)\s*=\s*\.\w+:0x([0-9A-Fa-f]+)", line)
        if m:
            syms[m.group(1)] = m.group(2).upper()
    return syms


# ─── CW mangling helpers ───────────────────────────────────────────────────────

def cw_extract_method_and_class(mangled: str) -> tuple[str, str] | None:
    """
    Try to extract (method_name, class_name) from a CW-mangled symbol.

    Handles:
      __ct__12AcNpcAprilFv          -> ('<ctor>', 'AcNpcApril')
      __dt__12AcNpcAprilFv          -> ('<dtor>', 'AcNpcApril')
      init__12AcNpcAprilFv          -> ('init', 'AcNpcApril')
      update__Q2_2Ac12AcNpcAprilFv  -> ('update', 'AcNpcApril')
    """
    # Split at first __ separator between method and class
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)__(.+)$', mangled)
    if not m:
        return None
    method_raw, class_part = m.group(1), m.group(2)
    method = '<ctor>' if method_raw == '__ct' else '<dtor>' if method_raw == '__dt' else method_raw

    # Q-qualified: Q2_2Ns12ClassName  or  Q3_2Ns2Ns12ClassName
    q_match = re.match(r'^Q\d_(?:\d+[A-Za-z][a-zA-Z0-9_]*)+', class_part)
    if q_match:
        parts = re.findall(r'(\d+)([A-Za-z][a-zA-Z0-9_]*)', class_part)
        if parts:
            cls_name = parts[-1][1]
            return (method, cls_name)

    # Simple: NlenClassName
    s_match = re.match(r'^(\d+)([A-Za-z][a-zA-Z0-9_]*)F', class_part)
    if s_match:
        length = int(s_match.group(1))
        cls_candidate = s_match.group(2)
        if length == len(cls_candidate):
            return (method, cls_candidate)

    return None


# ─── Build AC class context file ──────────────────────────────────────────────

def build_class_context(nl_data: dict) -> int:
    """Write data/ac_class_context.txt for LLM context injection."""
    categories = nl_data.get("categories", {})

    lines = [
        "/* Known Animal Crossing class names (from New Leaf RTTI, used in City Folk too) */",
        "",
    ]

    order = [
        ("ac_game",    "AcXxx — main AC game objects"),
        ("bs_system",  "BsXxx — base systems / managers"),
        ("npc",        "NpcXxx — NPC behaviour"),
        ("furniture",  "FtrXxx — furniture"),
        ("field",      "FldXxx — field/outdoor"),
        ("background", "BgXxx — background"),
        ("game_mgr",   "GmXxx — game managers"),
        ("ui",         "UiXxx — UI"),
        ("fish_system","FsXxx — fish system"),
        ("museum_alt", "MuXxx — museum"),
    ]

    for key, comment in order:
        items = categories.get(key, [])
        if not items:
            continue
        lines.append(f"// {comment} ({len(items)} classes)")
        # Group 8 per line
        for i in range(0, len(items), 8):
            chunk = items[i:i+8]
            lines.append("//   " + "  ".join(chunk))
        lines.append("")

    # Other (non-AC-prefixed) classes that still appear in the game
    other = categories.get("other", [])
    if other:
        lines.append(f"// Other shared classes ({len(other)} classes)")
        for i in range(0, min(len(other), 64), 8):
            chunk = other[i:i+8]
            lines.append("//   " + "  ".join(chunk))
        lines.append("")

    CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_FILE.write_text("\n".join(lines))
    return len(lines)


# ─── Symbol hints from CW mangling ────────────────────────────────────────────

def scan_cw_mangled_hints(symbols: dict[str, str], nl_classes: set[str],
                          existing_hints: dict, dry_run: bool) -> int:
    """
    Find fn_XXXXXXXX symbols whose CW-mangled name contains a known NL class.
    Emit a hint with confidence 0.65 (cross-game, same franchise).
    """
    added = 0
    for sym_name, addr in symbols.items():
        if not sym_name.startswith("fn_"):
            continue
        key = f"fn_{addr}"
        if key in existing_hints and existing_hints[key].get("confidence", 0) >= 0.65:
            continue
        result = cw_extract_method_and_class(sym_name)
        if not result:
            continue
        method, cls = result
        if cls not in nl_classes:
            continue

        if method in ('<ctor>', '<dtor>'):
            hint_name = f"{'__ct' if method == '<ctor>' else '__dt'}__{len(cls)}{cls}"
        else:
            hint_name = f"{method}__{len(cls)}{cls}"

        if not dry_run:
            existing_hints[key] = {
                "name": hint_name,
                "class": cls,
                "method": method,
                "source": "cross_game_nl_rtti",
                "confidence": 0.65,
            }
        added += 1
        if dry_run:
            print(f"  [dry] {key}: {hint_name}  (class={cls})")

    return added


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(nl_data: dict, symbols: dict, nl_classes: set, hints: dict):
    total_fn = sum(1 for s in symbols if s.startswith("fn_"))
    hinted   = sum(1 for h in hints.values() if h.get("source") == "cross_game_nl_rtti")
    print(f"\n  NL class names loaded : {len(nl_classes)}")
    print(f"  fn_* symbols in ROM   : {total_fn}")
    print(f"  Hints from NL RTTI    : {hinted}")

    cats = nl_data.get("categories", {})
    print(f"\n  NL class categories:")
    for cat, items in sorted(cats.items()):
        print(f"    {cat:20s}: {len(items)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cross-game symbol analysis for ACCF")
    parser.add_argument("--dry-run",  action="store_true", help="Show changes without writing")
    parser.add_argument("--context",  action="store_true", help="Rebuild ac_class_context.txt only")
    parser.add_argument("--report",   action="store_true", help="Print report and exit")
    args = parser.parse_args()

    nl_data = load_json(NL_SYMS_FILE)
    if not nl_data:
        print("  ⚠  data/nl_symbols.json not found — run with --extract first")
        sys.exit(1)

    nl_classes: set[str] = set(nl_data.get("all_classes", []))
    symbols    = load_symbols()
    hints      = load_json(HINTS_FILE) if HINTS_FILE.exists() else {}

    # Always rebuild context file (it's cheap)
    n_lines = build_class_context(nl_data)
    print(f"  📝  ac_class_context.txt rebuilt ({n_lines} lines)")

    if args.context:
        return

    if args.report:
        print_report(nl_data, symbols, nl_classes, hints)
        return

    # Scan for CW-mangled hints
    added = scan_cw_mangled_hints(symbols, nl_classes, hints, dry_run=args.dry_run)

    if added and not args.dry_run:
        save_hints(hints)
        print(f"  ✅  Added {added} cross-game hint(s) to symbol_hints.json")
    elif added:
        print(f"  ℹ  Would add {added} cross-game hint(s) (dry run)")
    else:
        print(f"  ℹ  No new cross-game hints to add")

    print_report(nl_data, symbols, nl_classes, hints)


if __name__ == "__main__":
    main()
