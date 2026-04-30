#!/usr/bin/env python3
"""
compiler_hints.py — Automatic compiler issue detection by analyzing SOURCE CODE.

Instead of trying to parse assembly diffs, we analyze the actual C/C++ source
for patterns known to cause compiler issues with mwcceppc:

1. Char signedness issues: unsigned char vs signed char declarations
2. Float operations: patterns that trigger FMA (fused multiply-add)
3. Inline complexity: functions with many nested calls
4. Type mismatches: implicit casts that vary by compiler version

Usage:
  python3 tools/compiler_hints.py --diagnose auto_03_802C5394_text
  python3 tools/compiler_hints.py --check-all
  python3 tools/compiler_hints.py --apply <unit> --fix unsigned-char
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
COMPILER_DB = DATA_DIR / "compiler_hints.json"
REPORT_JSON = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
SRC_DIR = PROJECT_ROOT / "src"


@dataclass
class CompilerDiagnosis:
    """Result of analyzing a function for compiler issues."""
    unit_name: str
    issue_type: str  # "char_signedness", "float_operations", "complexity", "unknown"
    confidence: float  # 0.0-1.0
    suggested_fix: str
    evidence: list[str]

    def __repr__(self):
        return f"{self.issue_type} ({self.confidence*100:.0f}%): {self.suggested_fix}"


def load_compiler_db() -> dict:
    """Load the compiler hints database."""
    if COMPILER_DB.exists():
        return json.loads(COMPILER_DB.read_text())
    return {
        "diagnoses": {},
        "fixes_applied": {},
        "escalation_ladder": [
            {"level": 0, "name": "baseline", "flags": [], "description": "Default mwcceppc 1.1 -O2"},
            {"level": 1, "name": "unsigned-char", "flags": ["-unsigned-char"], "description": "Force unsigned char"},
            {"level": 2, "name": "signed-char", "flags": ["-signed-char"], "description": "Force signed char"},
            {"level": 3, "name": "fp-off", "flags": ["-fp off"], "description": "Disable float contraction"},
            {"level": 4, "name": "inline-off", "flags": ["-inline off"], "description": "Disable inlining"},
            {"level": 5, "name": "o0", "flags": ["-O0"], "description": "No optimization"},
            {"level": 6, "name": "o1", "flags": ["-O1"], "description": "Basic optimization"},
        ],
    }


def save_compiler_db(db: dict):
    """Save the compiler hints database."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COMPILER_DB.write_text(json.dumps(db, indent=2))


def find_source_file(unit_name: str) -> Optional[Path]:
    """Find the C/C++ source file for a unit."""
    # Strip 'main/' prefix
    base_name = unit_name.replace("main/", "")

    for ext in (".c", ".cpp", ".cc", ".cxx"):
        for candidate in SRC_DIR.rglob(f"{base_name}{ext}"):
            return candidate

    return None


def analyze_source(source_file: Path) -> Optional[CompilerDiagnosis]:
    """Analyze source code for compiler issues."""
    if not source_file.exists():
        return None

    try:
        content = source_file.read_text()
    except Exception:
        return None

    evidence = []
    scores = {
        "char_signedness": 0.0,
        "float_operations": 0.0,
        "complexity": 0.0,
    }

    # ─── Check 1: Char signedness issues ───────────────────────────────────
    # If the file uses plain 'char' (not unsigned char), it might have issues

    # Count plain 'char' declarations
    plain_char_count = len(re.findall(r'\bchar\s+[\w\*]+\s*[=;,\)]', content))
    unsigned_char_count = len(re.findall(r'\bunsigned\s+char\b', content))
    signed_char_count = len(re.findall(r'\bsigned\s+char\b', content))

    if plain_char_count > 5 and unsigned_char_count == 0:
        evidence.append(f"Heavy use of plain 'char' ({plain_char_count} occurrences, no explicit signedness)")
        scores["char_signedness"] += 0.3

    if unsigned_char_count == 0 and signed_char_count == 0 and plain_char_count > 0:
        evidence.append("No explicit char type declarations (unsigned/signed char)")
        scores["char_signedness"] += 0.2

    # Check for char arrays used in arithmetic
    if re.search(r'char\s+\*?\w+.*[+\-*/%]', content):
        evidence.append("Plain char variables used in arithmetic operations")
        scores["char_signedness"] += 0.25

    # ─── Check 2: Float operations ────────────────────────────────────────

    float_count = len(re.findall(r'\bfloat\b|\bdouble\b', content))
    fma_pattern = re.findall(r'(\w+)\s*\*\s*(\w+)\s*[+\-]\s*(\w+)', content)  # a*b+c pattern

    if float_count > 3 and fma_pattern:
        evidence.append(f"Float operations with FMA-like patterns: {len(fma_pattern)} found")
        scores["float_operations"] += 0.3

    if re.search(r'float\s*\*|double\s*\*', content):
        evidence.append("Float/double pointers (may have precision issues)")
        scores["float_operations"] += 0.15

    # ─── Check 3: Code complexity ─────────────────────────────────────────

    # Count nested braces (rough complexity measure)
    max_nesting = 0
    current_nesting = 0
    for char in content:
        if char == '{':
            current_nesting += 1
            max_nesting = max(max_nesting, current_nesting)
        elif char == '}':
            current_nesting -= 1

    if max_nesting > 5:
        evidence.append(f"Deep nesting level ({max_nesting}), may cause optimization issues")
        scores["complexity"] += 0.2

    # Count function calls
    function_call_count = len(re.findall(r'\b\w+\s*\([^)]*\)', content))
    if function_call_count > 20:
        evidence.append(f"High function call density ({function_call_count})")
        scores["complexity"] += 0.15

    # ─── Determine primary issue ──────────────────────────────────────────

    primary_issue = max(scores, key=scores.get)
    confidence = scores[primary_issue]

    if confidence < 0.15:
        return None  # Not enough evidence

    # Map to suggested fix
    fix_map = {
        "char_signedness": "Explicit char types: unsigned char or signed char",
        "float_operations": "Check float precision or disable FMA (-fp off)",
        "complexity": "Try lower optimization level or inline settings",
    }

    unit_name = source_file.stem
    if unit_name.startswith("auto_"):
        unit_name = f"main/{unit_name}_text"

    return CompilerDiagnosis(
        unit_name=unit_name,
        issue_type=primary_issue,
        confidence=confidence,
        suggested_fix=fix_map[primary_issue],
        evidence=evidence,
    )


def check_all_units():
    """Scan all source files for compiler issues."""
    if not REPORT_JSON.exists():
        print("  ⚠  report.json not found — run 'ninja' first")
        return

    with open(REPORT_JSON) as f:
        report = json.load(f)

    units = report.get("units", [])
    units = [u for u in units if u.get("measures", {}).get("fuzzy_match_percent", 0) < 100]

    print(f"\n🔍  Scanning {len(units)} mismatched units for compiler issues...\n")

    issues_found = {}
    db = load_compiler_db()

    for i, unit in enumerate(units[:100], 1):  # Scan first 100 to avoid slowdown
        unit_name = unit.get("name", "")
        pct = unit.get("measures", {}).get("fuzzy_match_percent", 0)

        src_file = find_source_file(unit_name)
        if not src_file:
            continue

        diagnosis = analyze_source(src_file)
        if diagnosis:
            print(f"[{i}] {diagnosis.unit_name} ({pct:.1f}%)")
            print(f"    → {diagnosis}")
            for evidence in diagnosis.evidence[:2]:
                print(f"      • {evidence}")
            print()

            key = diagnosis.issue_type
            if key not in issues_found:
                issues_found[key] = []
            issues_found[key].append(unit_name)

            # Store in database
            db["diagnoses"][unit_name] = asdict(diagnosis)

    if not issues_found:
        print("  ℹ  No compiler issues detected in source code.")
    else:
        print(f"\n📊  Summary:")
        for issue_type, units_list in issues_found.items():
            print(f"  {issue_type}: {len(units_list)} units")

    save_compiler_db(db)


def diagnose_unit(unit_name: str) -> Optional[CompilerDiagnosis]:
    """Analyze a specific unit."""
    src_file = find_source_file(unit_name)
    if not src_file:
        print(f"  ⚠  Source file not found for {unit_name}")
        return None

    return analyze_source(src_file)


def apply_fix_to_unit(unit_name: str, fix_level: int) -> bool:
    """Apply a compiler fix to a unit."""
    db = load_compiler_db()
    if fix_level >= len(db["escalation_ladder"]):
        return False

    fix = db["escalation_ladder"][fix_level]
    src_file = find_source_file(unit_name)

    if not src_file:
        print(f"  ⚠  Could not find source file for {unit_name}")
        return False

    content = src_file.read_text()
    pragma_comment = f"\n// Compiler hint (level {fix_level}): {fix['description']}\n"
    pragma_comment += f"// Flags: {' '.join(fix['flags'])}\n"

    if f"level {fix_level}" not in content:
        content = pragma_comment + content
        src_file.write_text(content)

        if "fixes_applied" not in db:
            db["fixes_applied"] = {}
        db["fixes_applied"][unit_name] = {
            "fix_level": fix_level,
            "fix_name": fix["name"],
            "flags": fix["flags"],
        }
        save_compiler_db(db)

        print(f"  ✅  Applied {fix['name']} to {unit_name}")
        return True

    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnose", type=str, help="Diagnose a specific unit")
    parser.add_argument("--apply", type=str, help="Apply a fix to a unit")
    parser.add_argument("--fix", type=str, help="Which fix to apply (with --apply)")
    parser.add_argument("--check-all", action="store_true", help="Scan all units")
    args = parser.parse_args()

    if args.diagnose:
        diagnosis = diagnose_unit(args.diagnose)
        if diagnosis:
            print(f"\n🔍  Diagnosis for {args.diagnose}:")
            print(f"  Issue: {diagnosis}")
            print(f"  Confidence: {diagnosis.confidence*100:.0f}%")
            print(f"\n  Evidence:")
            for ev in diagnosis.evidence:
                print(f"    • {ev}")
            print(f"\n  Suggested Fix: {diagnosis.suggested_fix}")
        else:
            print(f"  ℹ  No compiler issue detected")

    elif args.apply and args.fix:
        try:
            fix_level = int(args.fix)
        except ValueError:
            db = load_compiler_db()
            fix_by_name = {f["name"]: f["level"] for f in db["escalation_ladder"]}
            fix_level = fix_by_name.get(args.fix)
            if fix_level is None:
                print(f"  ⚠  Unknown fix: {args.fix}")
                return 1

        if apply_fix_to_unit(args.apply, fix_level):
            print(f"  Next: rebuild with 'ninja'")
        else:
            print(f"  ⚠  Failed to apply fix")
            return 1

    elif args.check_all:
        check_all_units()

    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
