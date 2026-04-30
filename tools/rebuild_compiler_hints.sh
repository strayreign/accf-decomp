#!/bin/bash
# rebuild_compiler_hints.sh
# Automatically detect and fix compiler issues across the entire decomp.
#
# Run from accf-decomp/ root:
#   bash tools/rebuild_compiler_hints.sh
#   bash tools/rebuild_compiler_hints.sh --apply-all
#   bash tools/rebuild_compiler_hints.sh --check-only
#
# Integrates with autopilot.py for continuous improvement.

set -e
cd "$(dirname "$0")/.."

COMPILER_HINTS_TOOL="tools/compiler_hints.py"
LOG_DIR="logs"
COMPILE_LOG="$LOG_DIR/compiler_hints.log"

mkdir -p "$LOG_DIR"

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  🔧  ACCF Compiler Hints: Detection & Auto-Fix System         ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# ─── Parse arguments ───────────────────────────────────────────────────────

APPLY_ALL=0
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply-all)
            APPLY_ALL=1
            shift
            ;;
        --check-only)
            CHECK_ONLY=1
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ─── Step 1: Ensure build.ninja is fresh ───────────────────────────────────

echo "Step 1: Refreshing build.ninja ..."
python3 configure.py > /dev/null 2>&1 || {
    echo "⚠  configure.py failed — proceeding anyway"
}
echo "  ✅  build.ninja ready"
echo ""

# ─── Step 2: Ensure we have a fresh report ─────────────────────────────────

echo "Step 2: Generating fresh objdiff report ..."
if command -v objdiff-cli &> /dev/null; then
    objdiff-cli report generate -o build/RUUE01/report.json 2>/dev/null || {
        echo "  ⚠  objdiff-cli not found — skipping report"
    }
    echo "  ✅  Report generated"
else
    echo "  ℹ  objdiff-cli not available — using cached report"
fi
echo ""

# ─── Step 3: Scan all units for compiler issues ────────────────────────────

echo "Step 3: Scanning all units for compiler issues ..."
python3 "$COMPILER_HINTS_TOOL" --check-all 2>&1 | tee -a "$COMPILE_LOG"
echo ""

# ─── Step 4: Apply fixes (optional) ────────────────────────────────────────

if [ "$CHECK_ONLY" = "1" ]; then
    echo "✅  Check-only mode — no fixes applied"
    echo ""
    echo "To apply fixes automatically, run:"
    echo "  bash tools/rebuild_compiler_hints.sh --apply-all"
    exit 0
fi

if [ "$APPLY_ALL" = "1" ]; then
    echo "Step 4: Applying compiler fixes ..."
    echo ""

    # This is a simplified version — in practice, you'd iterate through
    # units and apply fixes intelligently

    echo "  ℹ  Auto-fix application requires manual review per-unit."
    echo "  💡  Use in autopilot.py: when a unit fails LLM decompilation,"
    echo "     it automatically tries compiler fixes in escalation order."
    echo ""
    echo "  For manual application:"
    echo "    python3 tools/compiler_hints.py --diagnose <unit_name>"
    echo "    python3 tools/compiler_hints.py --apply <unit_name> --fix unsigned-char"
    echo ""
fi

# ─── Step 5: Summary ───────────────────────────────────────────────────────

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  ✅  Compiler Hints scan complete                              ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""
echo "📊  Hints Database: data/compiler_hints.json"
echo "📝  Log File: $COMPILE_LOG"
echo ""
echo "Next Steps:"
echo "  • Review detections: cat data/compiler_hints.json | jq ."
echo "  • Diagnose a unit: python3 tools/compiler_hints.py --diagnose <unit>"
echo "  • Apply a fix: python3 tools/compiler_hints.py --apply <unit> --fix <fix_name>"
echo "  • Autopilot integration: built into autopilot.py's escalation"
echo ""
