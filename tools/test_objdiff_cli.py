#!/usr/bin/env python3
"""
test_objdiff_cli.py — Test objdiff-cli to understand its actual API.

Exit code -9 means the process is crashing. We need to figure out:
1. What commands objdiff-cli supports
2. What unit name format it expects
3. Whether it's even available
"""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OBJDIFF_CLI = PROJECT_ROOT / "build" / "tools" / "objdiff-cli"


def run_cmd(args, description=""):
    """Run a command and show output."""
    if description:
        print(f"\n{description}")
        print(f"  Command: {' '.join(str(a) for a in args)}")
    try:
        result = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        print(f"  Exit code: {result.returncode}")
        if result.stdout:
            print(f"  stdout:\n{result.stdout[:500]}")
        if result.stderr:
            print(f"  stderr:\n{result.stderr[:500]}")
        return result
    except subprocess.TimeoutExpired:
        print(f"  ⚠  TIMEOUT")
        return None
    except Exception as e:
        print(f"  ⚠  Exception: {e}")
        return None


print("🔍  Testing objdiff-cli...\n")

# Check 1: Does it exist?
print(f"Checking: {OBJDIFF_CLI}")
if not OBJDIFF_CLI.exists():
    print("  ❌ NOT FOUND")
    print("  Run 'ninja' first to build tools")
    sys.exit(1)

print(f"  ✅ EXISTS (size: {OBJDIFF_CLI.stat().st_size} bytes)")

# Check 2: Can we run it at all?
run_cmd([str(OBJDIFF_CLI)], "Test 1: Run with no arguments")

# Check 3: Help/version
run_cmd([str(OBJDIFF_CLI), "--help"], "Test 2: Try --help")
run_cmd([str(OBJDIFF_CLI), "-h"], "Test 3: Try -h")
run_cmd([str(OBJDIFF_CLI), "--version"], "Test 4: Try --version")

# Check 4: List subcommands (common CLI pattern)
run_cmd([str(OBJDIFF_CLI), "help"], "Test 5: Try 'help' subcommand")

# Check 5: Try different diff formats
run_cmd(
    [str(OBJDIFF_CLI), "diff", "main/auto_03_8022AF78_text"],
    "Test 6: diff with 'main/' prefix"
)

run_cmd(
    [str(OBJDIFF_CLI), "diff", "auto_03_8022AF78_text"],
    "Test 7: diff without 'main/' prefix"
)

run_cmd(
    [str(OBJDIFF_CLI), "diff", "--unit", "main/auto_03_8022AF78_text"],
    "Test 8: diff with --unit flag"
)

# Check 6: Try objdiff.json to see available units
objdiff_json = PROJECT_ROOT / "objdiff.json"
if objdiff_json.exists():
    print(f"\n📄 objdiff.json exists ({objdiff_json.stat().st_size} bytes)")
    import json
    with open(objdiff_json) as f:
        data = json.load(f)
    units = data.get("units", [])
    print(f"  Total units: {len(units)}")
    if units:
        first = units[0]
        print(f"  First unit name: {first.get('name', 'N/A')}")
        print(f"  First unit metadata: {first.get('metadata', {})}")

# Check 7: Try report.json
report_json = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
if report_json.exists():
    print(f"\n📊 report.json exists ({report_json.stat().st_size} bytes)")
    with open(report_json) as f:
        data = json.load(f)
    print(f"  Report keys: {list(data.keys())}")
    units = data.get("units", [])
    print(f"  Units in report: {len(units)}")
    if units:
        first = units[0]
        print(f"  First unit: {first.get('name', 'N/A')} ({first.get('measures', {}).get('fuzzy_match_percent', '?')}%)")

print("\n" + "="*60)
print("💡 ANALYSIS:")
print("="*60)
print("""
If Test 6-8 all return -9, objdiff-cli is crashing on diff commands.
Possible causes:
1. objdiff-cli doesn't have a 'diff' subcommand (check Test 2 --help output)
2. Unit name format is wrong (missing 'main/' or extra prefix)
3. objdiff-cli is a different tool than expected
4. objdiff-cli needs database/config to be initialized

ACTION:
Look at the --help output from Test 2 to see actual subcommands.
""")
