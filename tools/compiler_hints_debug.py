#!/usr/bin/env python3
"""
compiler_hints_debug.py — Debug version to see what objdiff is actually returning.

Run this to understand what pattern matching we need to do.

Usage:
  python3 tools/compiler_hints_debug.py                    # Sample a few units
  python3 tools/compiler_hints_debug.py --unit <name>      # Debug specific unit
  python3 tools/compiler_hints_debug.py --show-raw <name>  # Show raw objdiff output
  python3 tools/compiler_hints_debug.py --sample-size 10   # Try 10 units
"""

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_JSON = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
OBJDIFF_CLI = PROJECT_ROOT / "build" / "tools" / "objdiff-cli"


def get_units_from_report(limit: int = 10) -> list:
    """Get list of mismatched units from report.json."""
    if not REPORT_JSON.exists():
        print("  ⚠  report.json not found")
        return []

    with open(REPORT_JSON) as f:
        report = json.load(f)

    units = report.get("units", [])
    # Filter to mismatched, _text units
    mismatched = [
        u for u in units
        if u.get("measures", {}).get("fuzzy_match_percent", 0) < 100
        and "_text" in u.get("name", "")
    ]

    return mismatched[:limit]


def get_objdiff_output(unit_name: str) -> str:
    """Get raw objdiff diff output for a unit."""
    if not OBJDIFF_CLI.exists():
        print(f"  ⚠  objdiff-cli not found at {OBJDIFF_CLI}")
        return ""

    try:
        result = subprocess.run(
            [str(OBJDIFF_CLI), "diff", unit_name],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
        else:
            print(f"  ⚠  objdiff returned {result.returncode}")
            if result.stderr:
                print(f"     stderr: {result.stderr[:200]}")
            return ""
    except subprocess.TimeoutExpired:
        print(f"  ⚠  objdiff timed out")
        return ""
    except Exception as e:
        print(f"  ⚠  Exception: {e}")
        return ""


def analyze_diff(diff: str, unit_name: str) -> dict:
    """Analyze a diff and return stats."""
    stats = {
        "unit": unit_name,
        "diff_length": len(diff),
        "diff_lines": len(diff.split('\n')),
        "has_function_headers": "Function " in diff,
        "has_addresses": bool(set(c in diff for c in "0123456789abcdef")),
        "keyword_counts": {},
        "sample_lines": diff.split('\n')[:20],  # First 20 lines
    }

    # Count interesting keywords
    keywords = [
        "srwi", "srawi", "rlwinm", "srw", "sra",  # Shift ops
        "fmadd", "fmul", "fadd", "fsub",  # Float ops
        "sdata2", "sdata", "rodata",  # Sections
        "bl ", "bla", "nop",  # Calls
    ]

    for kw in keywords:
        count = diff.count(kw)
        if count > 0:
            stats["keyword_counts"][kw] = count

    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", type=str, help="Debug specific unit")
    parser.add_argument("--show-raw", type=str, help="Show raw objdiff output")
    parser.add_argument("--sample-size", type=int, default=5, help="Sample size (default 5)")
    args = parser.parse_args()

    print("🔧  Compiler Hints — Debug Mode\n")

    # Check if objdiff exists
    if not OBJDIFF_CLI.exists():
        print(f"❌ objdiff-cli not found at {OBJDIFF_CLI}")
        print(f"   Run 'ninja' first to build tools\n")
        return 1

    if args.show_raw:
        print(f"📋 Raw objdiff output for {args.show_raw}:\n")
        diff = get_objdiff_output(args.show_raw)
        if diff:
            print(diff[:2000])  # First 2000 chars
            if len(diff) > 2000:
                print(f"\n... ({len(diff) - 2000} more characters)")
        else:
            print("  (No output)")
        return 0

    if args.unit:
        print(f"🔍 Analyzing {args.unit}...\n")
        diff = get_objdiff_output(args.unit)
        if not diff:
            print("  (No output from objdiff)")
            return 1

        stats = analyze_diff(diff, args.unit)
        print(f"Diff length: {stats['diff_length']} chars")
        print(f"Diff lines: {stats['diff_lines']}")
        print(f"Has function headers: {stats['has_function_headers']}")
        print(f"\nKeyword counts:")
        for kw, count in sorted(stats['keyword_counts'].items(), key=lambda x: -x[1]):
            print(f"  {kw}: {count}")

        print(f"\nFirst 20 lines of diff:")
        for i, line in enumerate(stats['sample_lines'][:20], 1):
            print(f"  {i:2d}: {line[:100]}")

        return 0

    # Default: sample units
    print(f"📊 Sampling {args.sample_size} mismatched units...\n")

    units = get_units_from_report(args.sample_size)
    if not units:
        print("  No mismatched units found in report.json")
        print("  Run 'ninja' and 'objdiff report generate' first\n")
        return 1

    for i, unit in enumerate(units, 1):
        unit_name = unit.get("name", "")
        pct = unit.get("measures", {}).get("fuzzy_match_percent", 0)

        print(f"[{i}/{len(units)}] {unit_name} ({pct:.1f}%)")

        diff = get_objdiff_output(unit_name)
        if not diff:
            print(f"  ⚠  No objdiff output")
            continue

        stats = analyze_diff(diff, unit_name)

        print(f"  Diff: {stats['diff_length']} chars, {stats['diff_lines']} lines")

        if stats['keyword_counts']:
            top_kw = sorted(stats['keyword_counts'].items(), key=lambda x: -x[1])[:3]
            print(f"  Top keywords: {', '.join(f'{kw}({c})' for kw, c in top_kw)}")
        else:
            print(f"  ⚠  No recognized keywords in diff!")

        print()

    print("\n💡 Recommendations:")
    print("  • If diff_length is 0: objdiff is failing or unit is already matched")
    print("  • If keyword_counts is empty: pattern matching needs to be adjusted")
    print("  • Use --show-raw <unit> to see actual diff format")
    print("  • Use --unit <unit> to dive deep into one unit")

    return 0


if __name__ == "__main__":
    sys.exit(main())
