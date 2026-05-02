#!/usr/bin/env python3
"""
decomp_loop_compiler_hooks.py — Integration hooks for compiler_hints.py into decomp_loop.py

Called by decomp_loop.py when a function fails to match.
Detects compiler issues and suggests/applies fixes automatically.

Usage (internal to decomp_loop):
  from tools.decomp_loop_compiler_hooks import try_compiler_fix_escalation

  if not matched:
      should_retry, next_fix = try_compiler_fix_escalation(unit_name, attempt_num)
      if should_retry:
          # Re-run decomp with compiler flags applied
"""

import json
import sys
from pathlib import Path

from compiler_hints import (
    CompilerDiagnosis,
    diagnose_unit,
    suggest_escalation_path,
    apply_fix_to_unit,
    load_compiler_db,
    save_compiler_db,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CompilerFixEscalation:
    """Track compiler fix escalation across attempts."""

    def __init__(self, unit_name: str):
        self.unit_name = unit_name
        self.db = load_compiler_db()
        self.history = self.db.get("fixes_applied", {}).get(unit_name, {})
        self.current_level = self.history.get("fix_level", -1)

    def get_next_fix(self) -> dict | None:
        """Return the next compiler fix to try, or None if exhausted."""
        diagnosis = diagnose_unit(self.unit_name)
        if not diagnosis:
            return None

        escalation = suggest_escalation_path(self.unit_name, diagnosis.issue_type)

        # Find next untried level
        for fix in escalation:
            if fix["level"] > self.current_level:
                return fix

        return None  # All levels exhausted

    def try_next_fix(self) -> bool:
        """
        Apply the next compiler fix in the escalation path.
        Returns True if a fix was applied, False if none available or error.
        """
        next_fix = self.get_next_fix()
        if not next_fix:
            return False

        success = apply_fix_to_unit(self.unit_name, next_fix["level"])
        if success:
            self.current_level = next_fix["level"]
        return success

    def summary(self) -> str:
        """Return a human-readable summary of escalation status."""
        diagnosis = diagnose_unit(self.unit_name)
        if not diagnosis:
            return f"No compiler issue detected for {self.unit_name}"

        next_fix = self.get_next_fix()
        if next_fix:
            return f"{diagnosis.issue_type}: try level {next_fix['level']} ({next_fix['name']})"
        else:
            return f"{diagnosis.issue_type}: all fix levels exhausted"


def should_try_compiler_fix(unit_name: str, attempt_num: int, match_pct: float) -> bool:
    """
    Determine if we should try a compiler fix for this unit.

    Heuristics:
    - Only after LLM attempts have mostly failed (attempt_num >= 2)
    - Only if the function already has some matching code (match_pct >= 30%)
    - Skip if we've already tried many compiler fixes
    """
    if attempt_num < 2:
        return False  # Give LLM at least 2 shots first
    if match_pct < 30:
        return False  # Not enough matching code to diagnose
    if match_pct == 0:
        return False  # No code decompiled yet

    escalation = CompilerFixEscalation(unit_name)
    return escalation.get_next_fix() is not None


def try_compiler_fix_and_rebuild(unit_name: str, dry_run: bool = False) -> bool:
    """
    Attempt to apply a compiler fix and rebuild the unit.

    Returns True if a fix was applied (caller should re-run decompilation).
    Returns False if no fix available or application failed.
    """
    escalation = CompilerFixEscalation(unit_name)

    next_fix = escalation.get_next_fix()
    if not next_fix:
        return False

    print(f"\n  🔧  Compiler escalation: trying {next_fix['name']} (level {next_fix['level']})")
    print(f"      Rationale: {escalation.summary()}")

    if dry_run:
        print(f"      [DRY RUN] Would apply: {' '.join(next_fix['flags'])}")
        return False

    success = escalation.try_next_fix()
    if success:
        print(f"  ✅  Applied {next_fix['name']}")
        print(f"      Next: rebuild with 'ninja' and re-run decompilation")
        return True
    else:
        print(f"  ⚠  Failed to apply {next_fix['name']}")
        return False


def get_compiler_context_for_function(unit_name: str) -> str:
    """
    Return LLM context about known compiler issues for this function.
    Can be included in the decomp_loop prompt to guide code generation.
    """
    diagnosis = diagnose_unit(unit_name)
    if not diagnosis:
        return ""

    context = f"""
COMPILER NOTES:
- Detected issue: {diagnosis.issue_type}
- Confidence: {diagnosis.confidence*100:.0f}%
- Evidence: {'; '.join(diagnosis.evidence[:2])}
- Suggested fix: {diagnosis.suggested_fix}

This may affect:
"""

    if diagnosis.issue_type == "char_signedness":
        context += """- Shift instruction selection (srwi vs srawi)
- Arithmetic operations on bytes/characters
- Type casting and signedness comparisons
Consider using explicit 'unsigned char' or 'signed char' in declarations."""

    elif diagnosis.issue_type == "float_contraction":
        context += """- Floating-point operation ordering
- FMA (fused multiply-add) usage
- Precision and rounding differences
Consider keeping float operations in explicit order without combining them."""

    elif diagnosis.issue_type == "instruction_selection":
        context += """- Optimization level differences
- Inlining decisions
- Register allocation and instruction scheduling
Consider breaking up complex expressions into simpler statements."""

    return context


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 decomp_loop_compiler_hooks.py <unit_name>")
        sys.exit(1)

    unit = sys.argv[1]
    escalation = CompilerFixEscalation(unit)

    print(f"\n📊  Compiler Fix Status for {unit}:")
    print(f"  Current level: {escalation.current_level}")
    print(f"  {escalation.summary()}")
    print()

    diagnosis = diagnose_unit(unit)
    if diagnosis:
        print(f"  Evidence:")
        for ev in diagnosis.evidence:
            print(f"    • {ev}")
        print()

    next_fix = escalation.get_next_fix()
    if next_fix:
        print(f"  Next fix available:")
        print(f"    Level {next_fix['level']}: {next_fix['name']}")
        print(f"    Flags: {' '.join(next_fix['flags'])}")
        print(f"    Description: {next_fix['description']}")
        print()
        print(f"  To apply: python3 tools/compiler_hints.py --apply {unit} --fix {next_fix['name']}")
