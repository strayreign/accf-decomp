#!/usr/bin/env bash
# setup_ppc2cpp.sh — Build ppc2cpp from source and install it to build/tools/
# Run once from the project root:
#   bash tools/setup_ppc2cpp.sh
#
# Requirements (install via Homebrew if missing):
#   brew install cmake protobuf

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT="$PROJECT_ROOT/build/tools/ppc2cpp"

if [ -f "$OUT" ]; then
    echo "ppc2cpp already installed at $OUT"
    "$OUT" --version 2>/dev/null || true
    exit 0
fi

echo "=== Checking dependencies ==="
for dep in cmake protoc; do
    if ! command -v $dep &>/dev/null; then
        echo "Missing: $dep — run: brew install ${dep/protoc/protobuf}"
        exit 1
    fi
done
echo "cmake: $(cmake --version | head -1)"
echo "protoc: $(protoc --version)"

BUILD_TMP="$(mktemp -d)"
trap 'rm -rf "$BUILD_TMP"' EXIT

echo ""
echo "=== Cloning ppc2cpp ==="
git clone --depth=1 https://github.com/em-eight/ppc2cpp.git "$BUILD_TMP/ppc2cpp"

echo ""
echo "=== Configuring ==="
cmake -B "$BUILD_TMP/build" -S "$BUILD_TMP/ppc2cpp" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES="$(uname -m)" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5

echo ""
echo "=== Building (this takes ~1–2 min) ==="
cmake --build "$BUILD_TMP/build" --config Release -j"$(sysctl -n hw.logicalcpu)"

echo ""
echo "=== Installing ==="
mkdir -p "$PROJECT_ROOT/build/tools"
BUILT_BIN="$(find "$BUILD_TMP/build" -name "ppc2cpp" -type f | head -1)"
if [ -z "$BUILT_BIN" ]; then
    echo "ERROR: ppc2cpp binary not found after build"
    exit 1
fi
cp "$BUILT_BIN" "$OUT"
chmod +x "$OUT"

echo ""
echo "✓  ppc2cpp installed to build/tools/ppc2cpp"
"$OUT" --version 2>/dev/null || true
echo ""
echo "decomp_loop.py will automatically use it for semantic equivalence checks."
echo "No other setup needed — it activates whenever ppc2cpp is present."
