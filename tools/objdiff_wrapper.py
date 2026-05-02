#!/usr/bin/env python3
"""objdiff-cli wrapper for the ACCF decomp project.

Adapted from jurrejelle/ai-melee-decomp objdiff_wrapper.py.

Usage:
  python3 tools/objdiff_wrapper.py <symbol> [unit]
  python3 tools/objdiff_wrapper.py <symbol> [unit] --full
  python3 tools/objdiff_wrapper.py <symbol> [unit] --full-both
  python3 tools/objdiff_wrapper.py <symbol> [unit] --both-diff-only
  python3 tools/objdiff_wrapper.py <symbol> [unit] --sections

Symbol can be a function name (e.g. fn_802C5394) or partial match.
Unit can be omitted if you just want to search by symbol name.

Examples:
  python3 tools/objdiff_wrapper.py fn_802C5394
  python3 tools/objdiff_wrapper.py fn_802C5394 main/auto_03_802C5394_text
  python3 tools/objdiff_wrapper.py fn_802C5394 main/auto_03_802C5394_text --full-both

objdiff JSON layout:
  left  = "target"  (original binary, -1 side)
  right = "ours"    (compiled from source, -2 side)
  diff_kind values:
    DIFF_INSERT       — exists here but not on the other side
    DIFF_DELETE       — this side has something the other doesn't
    DIFF_ARG_MISMATCH — same opcode, different operand/reloc
    DIFF_REPLACE      — completely different opcode
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OBJDIFF_CLI  = PROJECT_ROOT / "build" / "tools" / "objdiff-cli"

try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass


# ── Formatting helpers ────────────────────────────────────────────────────────

def hex_addr(addr: int | str) -> str:
    try:
        return f"0x{int(addr):X}"
    except (ValueError, TypeError):
        return str(addr)


def format_inst(inst: dict[str, Any]) -> str:
    addr = hex_addr(inst.get("address", "?"))
    formatted = inst.get("formatted", "?")
    return f"{addr}: {formatted}"


# ── Instruction iterators ─────────────────────────────────────────────────────

def iter_instructions(sym: dict[str, Any]) -> list[dict[str, Any]]:
    return sym.get("instructions", []) or []


# ── Print modes ───────────────────────────────────────────────────────────────

def print_full(label: str, sym: dict[str, Any]) -> None:
    instrs = iter_instructions(sym)
    real_count = sum(1 for e in instrs if e.get("instruction"))
    diff_count = sum(1 for e in instrs if e.get("diff_kind"))
    print(f"\n   {label} ({real_count} instructions):")
    print(f"   {'-' * 60}")
    for entry in instrs:
        inst = entry.get("instruction")
        dk   = entry.get("diff_kind")
        if not inst:
            if dk:
                print(f"   >>> {'---':50s} <-- {dk} (gap)")
            continue
        line = format_inst(inst)
        if dk:
            print(f"   >>> {line:50s} <-- {dk}")
        else:
            print(f"       {line}")
    print(f"   {'-' * 60}")
    total = len(instrs)
    matched = total - diff_count
    print(f"   {matched}/{total} instructions match, {diff_count} differ")


def print_diff_only(label: str, sym: dict[str, Any]) -> None:
    instrs = iter_instructions(sym)
    diffs = [e for e in instrs if e.get("diff_kind")]
    print(f"\n   {label} ({len(diffs)} diff entries):")
    print(f"   {'-' * 60}")
    for entry in diffs:
        inst = entry.get("instruction")
        dk   = entry.get("diff_kind", "CHANGED")
        if not inst:
            print(f"   >>> {'---':50s} <-- {dk} (gap)")
        else:
            print(f"   >>> {format_inst(inst):50s} <-- {dk}")
    if not diffs:
        print("   (no differences)")
    print(f"   {'-' * 60}")


def print_paired_diff(ours_sym: dict[str, Any],
                      target_sym: dict[str, Any] | None,
                      full: bool) -> None:
    ours_instrs   = iter_instructions(ours_sym)
    target_instrs = iter_instructions(target_sym) if target_sym else []
    max_len = max(len(ours_instrs), len(target_instrs), 1)

    if full:
        print(f"\n   PAIRED ASSEMBLY ({max_len} rows):")
    else:
        print(f"\n   PAIRED DIFF:")
    print(f"   {'OURS':<44s}  {'TARGET':>44s}")
    print(f"   {'-' * 93}")

    diff_count = 0
    for i in range(max_len):
        ours_e = ours_instrs[i] if i < len(ours_instrs) else {}
        tgt_e  = target_instrs[i] if i < len(target_instrs) else {}

        ours_dk = ours_e.get("diff_kind")
        tgt_dk  = tgt_e.get("diff_kind")
        has_diff = bool(ours_dk or tgt_dk)

        if not full and not has_diff:
            continue

        diff_count += has_diff

        ours_inst = ours_e.get("instruction")
        tgt_inst  = tgt_e.get("instruction")

        ours_str = format_inst(ours_inst) if ours_inst else "---"
        tgt_str  = format_inst(tgt_inst) if tgt_inst else "---"

        if has_diff:
            dk_label = ours_dk or tgt_dk or "CHANGED"
            print(f"   >>> {ours_str:<42s}  |  {tgt_str:>42s}  [{dk_label}]")
        else:
            print(f"       {ours_str:<42s}  |  {tgt_str:>42s}")

    print(f"   {'-' * 93}")
    if not full:
        total   = max_len
        matched = total - diff_count
        print(f"   {matched}/{total} instructions match, {diff_count} differ")


# ── Build helpers ─────────────────────────────────────────────────────────────

def _unit_basename(unit: str) -> str:
    """Strip 'main/' prefix → bare name like auto_03_802C5394_text."""
    return unit.split("/")[-1]


def get_object_path(unit: str) -> Path:
    return PROJECT_ROOT / "build" / "RUUE01" / "obj" / (_unit_basename(unit) + ".o")


def get_source_path(unit: str) -> Path | None:
    base = _unit_basename(unit)
    src_dir = PROJECT_ROOT / "src"
    for ext in (".c", ".cpp", ".cp", ".cc"):
        p = src_dir / (base + ext)
        if p.exists():
            return p
    return None


def maybe_build_unit(unit: str) -> None:
    obj_path = get_object_path(unit)
    src_path = get_source_path(unit)

    if src_path and obj_path.exists():
        if src_path.stat().st_mtime > obj_path.stat().st_mtime:
            print(f"Source is newer than object — rebuilding {obj_path.name} …")
            rel = obj_path.relative_to(PROJECT_ROOT)
            result = subprocess.run(
                ["ninja", "-j1", str(rel)],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"  Built: {obj_path.name}")
            else:
                print("=== COMPILATION FAILED ===\n")
                print(result.stderr)
                sys.exit(result.returncode)
    elif not obj_path.exists():
        src_hint = str(src_path) if src_path else "(no source found)"
        print(f"Note: object not found: {obj_path}")
        print(f"  Source: {src_hint}")
        print("  Run `ninja` first, or check the unit name.")


# ── objdiff runner ────────────────────────────────────────────────────────────

def run_objdiff(symbol: str, unit: str | None) -> dict[str, Any]:
    if not OBJDIFF_CLI.exists():
        print(f"  ✖  objdiff-cli not found at {OBJDIFF_CLI}")
        print("     Run `make` or check build/tools/")
        sys.exit(1)

    cmd = [
        str(OBJDIFF_CLI), "diff",
        "-p", str(PROJECT_ROOT),
        "--format", "json",
        "--output", "-",
    ]
    if unit:
        cmd.extend(["-u", unit])
    cmd.append(symbol)

    print(f"Running objdiff-cli: {symbol}" + (f" [{unit}]" if unit else ""))
    print("-" * 60)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        sys.exit(result.returncode)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON output: {e}")
        print(result.stdout[:500])
        sys.exit(1)


# ── Unit lookup helpers ───────────────────────────────────────────────────────

def find_unit_for_symbol(symbol: str) -> str | None:
    """
    Try to infer the unit name from the symbol address.
    fn_802C5394 → looks for a unit whose name contains 802C5394.
    """
    addr_part = None
    # fn_XXXXXXXX or similar
    import re
    m = re.search(r'([0-9A-Fa-f]{8})', symbol)
    if m:
        addr_part = m.group(1).upper()

    if addr_part is None:
        return None

    objdiff_cfg = PROJECT_ROOT / "objdiff.json"
    if not objdiff_cfg.exists():
        return None

    with open(objdiff_cfg) as f:
        cfg = json.load(f)

    for unit in cfg.get("units", []):
        name = unit.get("name", "")
        if addr_part in name.upper():
            return name

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="objdiff assembly diff viewer for ACCF",
        add_help=True,
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full",           action="store_true",
                      help="Full assembly for our compiled side (with diff markers).")
    mode.add_argument("--full-both",      action="store_true",
                      help="Full paired side-by-side assembly (ours vs target).")
    mode.add_argument("--both-diff-only", action="store_true",
                      help="Paired diff showing only mismatching rows.")
    mode.add_argument("--sections",       action="store_true",
                      help="Section-level match percentages only.")
    ap.add_argument("symbol", help="Symbol/function name or address fragment")
    ap.add_argument("unit",   nargs="?", default=None,
                    help="Unit name, e.g. main/auto_03_802C5394_text (auto-detected if omitted)")
    args = ap.parse_args()

    unit = args.unit
    if unit is None:
        unit = find_unit_for_symbol(args.symbol)
        if unit:
            print(f"  Auto-detected unit: {unit}")

    if unit:
        maybe_build_unit(unit)

    data = run_objdiff(args.symbol, unit)

    left  = data.get("left",  {})   # target (original binary)
    right = data.get("right", {})   # ours   (compiled from source)

    ours_symbols   = right.get("symbols", [])
    target_symbols = left.get("symbols", [])

    target_sym_map: dict[str, dict[str, Any]] = {
        ts.get("name", ""): ts for ts in target_symbols if ts.get("name")
    }

    # ── Sections mode ─────────────────────────────────────────────────────────
    if args.sections:
        ours_sections = right.get("sections", [])
        print("\n=== SECTION SUMMARY ===")
        for s in ours_sections:
            mp = s.get("match_percent")
            if mp is not None:
                status = "✓" if mp == 100.0 else "✗"
                print(f"  {status} {s['name']}: {mp:.1f}%")
        print()
        return

    # ── Symbol match summary ──────────────────────────────────────────────────
    query = args.symbol.lower()
    matching = [s for s in ours_symbols if query in s.get("name", "").lower()]

    print("\n=== SYMBOL MATCH SUMMARY ===\n")

    if not matching:
        print(f"No symbols found matching '{args.symbol}'")
        if ours_symbols:
            print("\nAvailable symbols:")
            for s in ours_symbols[:20]:
                name  = s.get("name", "?")
                match = s.get("match_percent", 0)
                status = "✓" if match == 100.0 else "✗"
                print(f"  {status} {name} ({match:.1f}%)")
            if len(ours_symbols) > 20:
                print(f"  … and {len(ours_symbols) - 20} more")
        sys.exit(1)

    for sym in matching:
        name      = sym.get("name", "?")
        match     = sym.get("match_percent", 0)
        raw_addr  = sym.get("address")
        size      = sym.get("size", "?")
        target_sym = target_sym_map.get(name)
        flags     = (target_sym or sym).get("flags", 0)
        sym_type  = "FUNC" if flags == 1 else "DATA" if flags == 2 else "UNK"
        status    = "✓" if match == 100.0 else "✗"

        print(f"{status} {name} [{sym_type}]")
        if raw_addr is not None:
            print(f"   Address: {hex_addr(raw_addr)}  Size: {size} bytes  Match: {match:.1f}%")
        else:
            print(f"   Size: {size} bytes  Match: {match:.1f}%")

        if "instructions" in sym:
            if args.full_both:
                print_paired_diff(sym, target_sym, full=True)
            elif args.both_diff_only:
                print_paired_diff(sym, target_sym, full=False)
            elif args.full:
                print_full("OUR ASSEMBLY", sym)
            else:
                # Default: paired diff-only (most useful for iteration)
                print_paired_diff(sym, target_sym, full=False)

        print()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        os._exit(0)
