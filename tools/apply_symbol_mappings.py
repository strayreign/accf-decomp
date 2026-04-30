#!/usr/bin/env python3
"""
apply_symbol_mappings.py — Apply objdiff symbol_mappings to symbols.txt.

When you manually map a symbol from your decompiled object to the original
in objdiff's UI (e.g. renaming bar__Fl to match foo__Fs), this script reads
those mappings from objdiff.json and writes them back to symbols.txt.

Adapted from doldecomp/mkw/tools/apply_symbol_mappings.py.

Usage:
  python3 tools/apply_symbol_mappings.py           # apply + clear mappings
  python3 tools/apply_symbol_mappings.py --dry-run # show changes, don't write
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT  = Path(__file__).resolve().parent.parent
OBJDIFF_JSON  = PROJECT_ROOT / "objdiff.json"
SYMBOLS_FILES = [
    PROJECT_ROOT / "config" / "RUUE01" / "symbols.txt",
]


def load_mappings() -> dict[str, str]:
    """Read symbol_mappings from every unit in objdiff.json."""
    if not OBJDIFF_JSON.exists():
        print("  ⚠  objdiff.json not found — run configure.py first")
        return {}

    with open(OBJDIFF_JSON) as f:
        config = json.load(f)

    mappings: dict[str, str] = {}
    for unit in config.get("units", []):
        sm = unit.get("symbol_mappings")
        if sm:
            mappings.update(sm)

    return mappings


def apply_to_file(path: Path, mappings: dict[str, str], dry_run: bool) -> int:
    if not path.exists():
        return 0

    lines   = path.read_text().splitlines()
    updated = []
    changes = 0

    for line in lines:
        tokens = line.split()
        if tokens:
            old_sym = tokens[0]
            new_sym = mappings.get(old_sym)
            if new_sym is not None and new_sym != old_sym:
                if dry_run:
                    print(f"  [dry] {path.name}: {old_sym!r} → {new_sym!r}")
                else:
                    tokens[0] = new_sym
                    line = " ".join(tokens)
                changes += 1
        updated.append(line)

    if not dry_run and changes:
        path.write_text("\n".join(updated) + "\n")
        print(f"  ✏   {path.name}: {changes} symbol(s) renamed")

    return changes


def clear_mappings():
    """Remove all symbol_mappings keys from objdiff.json after applying."""
    with open(OBJDIFF_JSON) as f:
        config = json.load(f)

    cleared = 0
    for unit in config.get("units", []):
        if "symbol_mappings" in unit:
            del unit["symbol_mappings"]
            cleared += 1

    with open(OBJDIFF_JSON, "w") as f:
        json.dump(config, f, indent=2)

    if cleared:
        print(f"  🧹  Cleared symbol_mappings from {cleared} unit(s) in objdiff.json")


def main():
    parser = argparse.ArgumentParser(
        description="Apply objdiff symbol_mappings to symbols.txt"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    parser.add_argument("--keep-mappings", action="store_true",
                        help="Don't clear symbol_mappings from objdiff.json after applying")
    args = parser.parse_args()

    mappings = load_mappings()
    if not mappings:
        print("  ℹ  No symbol_mappings found in objdiff.json — nothing to apply")
        return

    print(f"  📌  {len(mappings)} symbol mapping(s) found")

    total = 0
    for path in SYMBOLS_FILES:
        total += apply_to_file(path, mappings, args.dry_run)

    if total == 0:
        print("  ℹ  No matching symbols found to rename")
    elif not args.dry_run:
        print(f"  ✅  Applied {total} rename(s)")
        if not args.keep_mappings:
            clear_mappings()


if __name__ == "__main__":
    main()
