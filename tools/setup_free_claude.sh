#!/usr/bin/env bash
# setup_free_claude.sh — Set up the free-claude-code proxy (Ollama backend)
#
# Routes Claude API calls (Haiku/Sonnet) through your already-running Ollama
# instance — completely free, no API key needed.
#
# Usage (one-time):
#   bash tools/setup_free_claude.sh
#
# Add to ~/.zshrc to enable permanently:
#   export ANTHROPIC_BASE_URL=http://localhost:8082
#   export ANTHROPIC_AUTH_TOKEN=freecc
#
# The proxy is started automatically by autopilot.py when
# ANTHROPIC_BASE_URL=http://localhost:8082 is set.

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROXY_DIR="$PROJECT_ROOT/build/tools/free-claude-code"

echo "=== free-claude-code proxy setup (Ollama backend) ==="
echo ""

# ── uv check ──────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    # Try common install locations first
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -f "$candidate" ]; then
            export PATH="$(dirname "$candidate"):$PATH"
            break
        fi
    done
fi

if ! command -v uv &>/dev/null; then
    echo "Installing uv …"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
echo "uv: $(uv --version)"

# ── Python 3.14 ───────────────────────────────────────────────────────────────
echo "Ensuring Python 3.14 is available …"
uv python install 3.14 2>&1 | grep -v "^$" | tail -3 || true

# ── Clone or update ───────────────────────────────────────────────────────────
if [ -d "$PROXY_DIR/.git" ]; then
    echo "✓  Repo already at $PROXY_DIR — pulling …"
    git -C "$PROXY_DIR" pull --ff-only 2>/dev/null || echo "   (already up to date)"
else
    echo "Cloning free-claude-code …"
    git clone --depth=1 https://github.com/Alishahryar1/free-claude-code.git "$PROXY_DIR"
fi

# ── Install Python deps ───────────────────────────────────────────────────────
echo "Installing Python dependencies (uv sync) …"
cd "$PROXY_DIR"
uv sync --frozen 2>&1 | tail -5

# ── Write .env ────────────────────────────────────────────────────────────────
ENV_FILE="$PROXY_DIR/.env"
echo "Writing .env (Ollama backend) …"
cp "$PROXY_DIR/.env.example" "$ENV_FILE"

# Route all Claude model slots to Ollama (already running for local models)
# Haiku → qwen2.5-coder:7b (fast, good for simple functions)
# Sonnet → deepseek-coder-v2:16b (stronger, for harder functions)
cat >> "$ENV_FILE" << 'EOENV'

# ── ACCF decomp overrides ──────────────────────────────────────────────────────
OLLAMA_BASE_URL="http://localhost:11434"
# 8GB M4 Air: 7B Q4 (~4.5GB) is the largest that fits comfortably.
# Both Claude slots go to qwen2.5-coder:7b — best 7B coder model in Ollama.
MODEL_HAIKU="ollama/qwen2.5-coder:7b"
MODEL_SONNET="ollama/qwen2.5-coder:7b"
MODEL_OPUS="ollama/qwen2.5-coder:7b"
MODEL="ollama/qwen2.5-coder:7b"
ANTHROPIC_AUTH_TOKEN=freecc
MESSAGING_PLATFORM="none"
EOENV

echo "✓  .env written"

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Add to ~/.zshrc:"
echo "    export ANTHROPIC_BASE_URL=http://localhost:8082"
echo "    export ANTHROPIC_AUTH_TOKEN=freecc"
echo ""
echo "  autopilot.py will start the proxy automatically."
echo "  Claude Haiku calls → qwen2.5-coder:7b (Ollama, free)"
echo "  Claude Sonnet calls → deepseek-coder-v2:16b (Ollama, free)"
