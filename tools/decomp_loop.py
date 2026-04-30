#!/usr/bin/env python3
"""
decomp_loop.py — Automated decompilation loop for ACCF (RUUE01)

Usage:
  python3 tools/decomp_loop.py 802C5394
  python3 tools/decomp_loop.py auto_03_802C5394_text
  python3 tools/decomp_loop.py 802C5394 --max-attempts 6
  python3 tools/decomp_loop.py 802C5394 --dry-run


Smart model ladder (adaptive):
  - Picks starting model based on function complexity + past match history
  - Steps up to stronger models on failure
  - Records results so future similar functions start on the proven model
  - Automatically downgrades when a cheaper model handles the job

Models (cheapest → strongest):
  Windows profile:
  0: Qwen2.5-Coder 7B Q4   — GTX 1060, fast local pre-screener
  1: Qwen2.5-Coder 14B Q3  — RTX 5060 Ti, primary local model
  2: Devstral Small 2 24B  — dual-GPU dense, local heavy lifter
  3: Qwen3 30B A3B Q4      — dual-GPU MoE, local reasoning alt
  4: Claude Haiku           — paid API, fast cloud fallback
  5: Claude Sonnet          — paid API, strongest fallback

Requirements:
  pip install httpx anthropic
  export CLAUDE_API_KEY=sk-ant-...
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT  = Path(__file__).resolve().parent.parent
SRC_DIR       = PROJECT_ROOT / "src"
ASM_DIR       = PROJECT_ROOT / "build" / "RUUE01" / "asm"
REPORT_JSON   = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
SYMBOLS_FILE  = PROJECT_ROOT / "config" / "RUUE01" / "symbols.txt"
DATA_DIR      = PROJECT_ROOT / "data"
_EXE          = ".exe" if sys.platform == "win32" else ""
OBJDIFF_CLI   = PROJECT_ROOT / "build" / "tools" / f"objdiff-cli{_EXE}"
PPC2CPP_CLI   = PROJECT_ROOT / "build" / "tools" / f"ppc2cpp{_EXE}"
HISTORY_FILE    = PROJECT_ROOT / "tools" / "model_history.json"
COST_FILE       = PROJECT_ROOT / "tools" / "claude_spend.json"
STRATEGY_NOTES  = PROJECT_ROOT / "data" / "strategy_notes.txt"
RESOURCES_DIR   = PROJECT_ROOT / "data" / "scraped"

# ppc2cpp project cache: created once from the target DOL, reused per session
_PPC2CPP_TARGET_PROJECT: Path | None = None

# ─── Self-improvement: strategy notes ────────────────────────────────────────
# Accumulated tips from previous runs (what worked, what didn't).
# Loaded once per session and injected into every prompt.
_STRATEGY_NOTES_CACHE: str = ""

def _load_strategy_notes() -> str:
    global _STRATEGY_NOTES_CACHE
    if _STRATEGY_NOTES_CACHE:
        return _STRATEGY_NOTES_CACHE
    if STRATEGY_NOTES.exists():
        try:
            text = STRATEGY_NOTES.read_text(encoding="utf-8").strip()
            if text:
                _STRATEGY_NOTES_CACHE = text
        except Exception:
            pass
    return _STRATEGY_NOTES_CACHE


def append_strategy_note(note: str):
    """Persist a lesson learned so future sessions benefit from it."""
    try:
        STRATEGY_NOTES.parent.mkdir(parents=True, exist_ok=True)
        existing = STRATEGY_NOTES.read_text(encoding="utf-8") if STRATEGY_NOTES.exists() else ""
        # Deduplicate — don't add the same note twice
        if note.strip() in existing:
            return
        with open(STRATEGY_NOTES, "a", encoding="utf-8") as f:
            f.write(f"\n- {note.strip()}")
        global _STRATEGY_NOTES_CACHE
        _STRATEGY_NOTES_CACHE = ""   # invalidate cache
    except Exception:
        pass

# ─── Session-level Claude availability flag ──────────────────────────────────
# Set to True the first time a Claude call returns 401/403 so we stop hammering
# a broken proxy on every subsequent function.
_CLAUDE_UNAVAILABLE: bool = False

# ─── Cost tracking ────────────────────────────────────────────────────────────
# Prices in USD per 1M tokens (as of 2025-Q4)
_CLAUDE_PRICES = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
}
# Hard cap: stop calling Claude if cumulative spend this session exceeds this
CLAUDE_SPEND_CAP_USD = float(os.environ.get("CLAUDE_SPEND_CAP", "1.00"))

_session_spend: float = 0.0   # USD spent this process invocation


def _record_claude_cost(model_name: str, input_tokens: int, output_tokens: int) -> float:
    """Track cost, print a warning if we're getting expensive. Returns cost of this call."""
    global _session_spend
    prices = _CLAUDE_PRICES.get(model_name, {"input": 3.00, "output": 15.00})
    cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
    _session_spend += cost

    # Persist to disk so costs survive restarts
    try:
        spend = json.loads(COST_FILE.read_text()) if COST_FILE.exists() else {}
        spend["total_usd"]       = spend.get("total_usd", 0.0) + cost
        spend["session_usd"]     = _session_spend
        spend["last_model"]      = model_name
        spend["last_input_tok"]  = input_tokens
        spend["last_output_tok"] = output_tokens
        COST_FILE.write_text(json.dumps(spend, indent=2))
    except Exception:
        pass

    total = spend.get("total_usd", 0.0) if COST_FILE.exists() else _session_spend
    print(f"  💰  +${cost:.4f}  (session ${_session_spend:.4f} / all-time ${total:.4f})",
          flush=True)
    return cost

# ─── Model definitions ────────────────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434/api/generate"

# ── Hardware profile ──────────────────────────────────────────────────────────
# Set DECOMP_PROFILE=windows to use the Windows/GPU model ladder.
# Default (unset / "mac") uses 7B models suitable for 8 GB unified memory.
_PROFILE = os.environ.get("DECOMP_PROFILE", "mac").lower()
_via_proxy = bool(os.environ.get("ANTHROPIC_BASE_URL"))

if _PROFILE == "windows":
    # Ryzen 7700X | RTX 5060 Ti 8GB GDDR7 (GPU 0) + GTX 1060 6GB GDDR5 (GPU 1) | 64GB DDR5
    #
    # GPU layout (dual independent Ollama instances):
    #   Port 11435  CUDA_VISIBLE_DEVICES=1  GTX 1060   → qwen2.5-coder:7b  Q4_K_M  ~4.7GB  pre-screener
    #   Port 11434  CUDA_VISIBLE_DEVICES=0  5060 Ti    → qwen2.5-coder:14b Q3_K_M  ~7.0GB  primary (full GPU)
    #   Port 11434  both GPUs             combined     → devstral-small-2:24b Q4    ~14GB   dense heavy lifter
    #   Port 11434  both GPUs             combined     → qwen3:30b-a3b-q4_K_M       ~19GB   MoE reasoning alt (5GB CPU spill)
    #
    # 14B at Q3 (not Q4) keeps the full model in the 5060 Ti's VRAM — Q4 at 9GB would spill
    # ~1GB onto CPU, losing the GDDR7 bandwidth advantage.
    # Devstral is dense (all 24B active per token) → better at strict rule-following.
    # Qwen3-Coder MoE fires only 3.3B active → faster, stronger on reasoning-heavy units.
    MODELS = [
        {"id": "qwen7b",     "backend": "ollama",
         "name":  "qwen2.5-coder:7b-instruct-q4_K_M",
         "url":   "http://localhost:11435/api/generate",
         "label": "Qwen2.5-Coder 7B Q4  (GTX 1060, free)"},
        {"id": "qwen14b",    "backend": "ollama",
         "name":  "qwen2.5-coder:14b-instruct-q3_K_M",
         "url":   "http://localhost:11434/api/generate",
         "label": "Qwen2.5-Coder 14B Q3 (RTX 5060 Ti, free)"},
        {"id": "devstral",   "backend": "ollama",
         "name":  "devstral-small-2:24b",
         "url":   "http://localhost:11434/api/generate",
         "label": "Devstral Small 2 24B (dual-GPU dense, free)"},
        {"id": "qwen3coder", "backend": "ollama",
         "name":  "qwen3:30b-a3b-q4_K_M",
         "url":   "http://localhost:11434/api/generate",
         "label": "Qwen3 30B A3B Q4     (dual-GPU MoE, free)"},
        {"id": "haiku",      "backend": "claude",
         "name":  "claude-haiku-4-5-20251001",
         "label": "Claude Haiku         (paid API)"},
        {"id": "sonnet",     "backend": "claude",
         "name":  "claude-sonnet-4-6",
         "label": "Claude Sonnet        (paid API)"},
    ]
else:
    # M4 MacBook Air 8 GB — largest comfortable model is 7B Q4 (~4.5 GB)
    MODELS = [
        {"id": "qwen",  "backend": "ollama",
         "name":  "qwen2.5-coder:7b",
         "label": "Qwen2.5-Coder 7B    (free)"},
        {"id": "gemma", "backend": "ollama",
         "name":  "codegemma:7b",
         "label": "CodeGemma 7B        (free)"},
        {"id": "haiku",  "backend": "claude",
         "name":  "claude-haiku-4-5-20251001",
         "label": "Claude Haiku        (→ Qwen via proxy)" if _via_proxy else "Claude Haiku        (paid)"},
        {"id": "sonnet", "backend": "claude",
         "name":  "claude-sonnet-4-6",
         "label": "Claude Sonnet       (→ Qwen via proxy)" if _via_proxy else "Claude Sonnet       (paid)"},
    ]

MODEL_IDS = [m["id"] for m in MODELS]

# ── Parallel worker config ─────────────────────────────────────────────────────
# Windows dual-GPU: run 2 workers simultaneously — one per GPU/Ollama instance.
# Worker A → port 11435 (GTX 1060, 7B screener).
# Worker B → port 11434 (RTX 5060 Ti, 14B/devstral primary).
# For heavy models (devstral/qwen3) that span both GPUs, single-worker mode applies.
if _PROFILE == "windows":
    _WORKER_URLS = [
        "http://localhost:11435/api/generate",   # GTX 1060
        "http://localhost:11434/api/generate",   # RTX 5060 Ti
    ]
    _PARALLEL_FN_WORKERS = 2
else:
    _WORKER_URLS = ["http://localhost:11434/api/generate"]
    _PARALLEL_FN_WORKERS = 1

# Per-worker model assignment for parallel mode (only when both tiers fit in VRAM)
_WORKER_MODELS = {
    "http://localhost:11435/api/generate": "qwen2.5-coder:7b-instruct-q4_K_M",
    "http://localhost:11434/api/generate": "qwen2.5-coder:14b-instruct-q3_K_M",
}

# Cached ninja target prefix (src or obj — depends on configure.py version)
_TARGET_PREFIX: str | None = None
# Per-unit prefix cache: some units live under src/, others under obj/
_UNIT_PREFIX_CACHE: dict[str, str] = {}

def _get_target_prefix(unit_name: str | None = None) -> str:
    """
    Return the ninja target prefix (src or obj) for a specific unit.
    Looks up the exact target in the ninja target list — different units
    can live under different prefixes, so never cache a single global answer.
    """
    if unit_name:
        if unit_name in _UNIT_PREFIX_CACHE:
            return _UNIT_PREFIX_CACHE[unit_name]
        r = subprocess.run(["ninja", "-t", "targets"], cwd=PROJECT_ROOT,
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            target = line.split(":")[0].strip()
            if unit_name in target and target.endswith(".o"):
                if "/src/" in target:
                    _UNIT_PREFIX_CACHE[unit_name] = "src"
                    return "src"
                if "/obj/" in target:
                    _UNIT_PREFIX_CACHE[unit_name] = "obj"
                    return "obj"
        # fallback: check which path actually exists on disk
        if (PROJECT_ROOT / "build" / "RUUE01" / "obj" / f"{unit_name}.o").exists():
            _UNIT_PREFIX_CACHE[unit_name] = "obj"
            return "obj"
        _UNIT_PREFIX_CACHE[unit_name] = "src"
        return "src"

    # No unit given — return a global default (legacy callers)
    global _TARGET_PREFIX
    if _TARGET_PREFIX is None:
        r = subprocess.run(["ninja", "-t", "targets"], cwd=PROJECT_ROOT,
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            t = line.split(":")[0].strip()
            if "auto_" in t and t.endswith(".o"):
                _TARGET_PREFIX = "src" if "/src/" in t else "obj"
                break
        else:
            _TARGET_PREFIX = "obj"
    return _TARGET_PREFIX

# ─── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a PowerPC reverse engineer decompiling Animal Crossing: City Folk (Wii, RUUE01).

    Compiler:  mwcceppc.exe  GC/1.3.2
    Flags:     -O4,p -inline auto -proc gekko -enum int -fp hardware
               -Cpp_exceptions off -RTTI off -fp_contract on -str reuse -multibyte

    Hard rules — violating any of these causes a compile mismatch:
    1. `char` is UNSIGNED. Never add a signed-char cast.
    2. `srwi` = logical right shift on unsigned type (>> on u32/u16/u8).
       `srawi` = arithmetic right shift on signed type (>> on s32).
       Do NOT mix them.
    3. No `long long`, no `int64_t`. ptrdiff_t is 32-bit.
    4. No `__attribute__`, `__asm__`, or any GCC extension.
    5. Output ONLY valid C or C++ source. No markdown fences, no comments explaining your logic.
    6. Include ONLY headers that are needed. Always start with #include <dolphin/types.h>.
       Types available: u8 u16 u32 u64 s8 s16 s32 s64 f32 f64.
    7. Keep all `extern` declarations for unresolved symbols (fn_XXXXXXXX, lbl_XXXXXXXX).
    8. A tail call in PPC (branch `b` to a function without `bl`) must be a `return fn(...)`.
    9. Pointer arithmetic with a byte offset MUST cast to `u8*` first:
       WRONG: `*(u16*)(ptr + 2)`  RIGHT: `*(u16*)((u8*)ptr + 2)`
   10. The project compiles with `-lang=c` (C89). Do NOT use `//` comments — use `/* */` only.
   11. Do NOT use CodeWarrior/PPC intrinsics as C functions. These are NOT valid C and will
       fail to compile: stw(), lwz(), lwzx(), lhz(), lbz(), sth(), stb(), blr(), extrwi(),
       rlwinm(), mflr(), mtlr(), stmw(), lmw(), or any other PPC instruction name.
       Express the same operation using normal C: pointer dereferences, casts, arithmetic.
   12. Do NOT use PPC register names as C identifiers. r0–r31, f0–f31, cr0–cr7,
       lr, ctr, xer, sp are NOT valid C variable names in mwcceppc — it treats
       them as reserved. Use names like val, tmp, offset, param1, result instead.
   13. The compiled output must be a BYTE-FOR-BYTE match of the provided .s file.

## PPC (Gekko/750) → C translation cheat-sheet
These mnemonics appear in .s files. They are NOT C functions — express them using
C operators, casts, and pointer dereferences ONLY.

Load/store (pointer dereferences with explicit u8* byte-offset cast):
  lwz  rD,N(rA)   →  val = *(u32*)((u8*)rA + N)
  lhz  rD,N(rA)   →  val = *(u16*)((u8*)rA + N)    /* unsigned half-word */
  lha  rD,N(rA)   →  val = *(s16*)((u8*)rA + N)    /* signed half-word */
  lbz  rD,N(rA)   →  val = *(u8*)((u8*)rA + N)
  lfs  fD,N(rA)   →  fval = *(f32*)((u8*)rA + N)
  lfd  fD,N(rA)   →  fval = *(f64*)((u8*)rA + N)
  stw  rS,N(rA)   →  *(u32*)((u8*)rA + N) = val
  sth  rS,N(rA)   →  *(u16*)((u8*)rA + N) = val
  stb  rS,N(rA)   →  *(u8*)((u8*)rA + N) = val
  stfs fS,N(rA)   →  *(f32*)((u8*)rA + N) = fval
  lwzx/lhzx/lbzx  →  indexed: *(u32*)((u8*)base + idx)   (same casts apply)
  stwu rS,N(rA)   →  *(u32*)((u8*)rA + N) = val; rA += N  (compiler handles)
  lmw/stmw        →  compiler emits these for multi-reg; never write them in C

Shift & rotate (use C shift/mask operators):
  srwi  rA,rB,N   →  (u32)rB >> N
  srawi rA,rB,N   →  (s32)rB >> N
  slwi  rA,rB,N   →  (u32)rB << N
  rlwinm rA,rB,SH,MB,ME  →  rotate-left-then-mask; usually one of:
                              (rB << SH) & MASK   or   (rB >> SH) & MASK
  extrwi rA,rB,N,B       →  (rB >> (32 - (B) - (N))) & ((1u << (N)) - 1)
  rlwimi rA,rS,SH,MB,ME  →  dst = (dst & ~MASK) | ((src << SH) & MASK)
  cntlzw rA,rB           →  __cntlzw(rB)  (mwcceppc built-in, NOT a PPC call)

Arithmetic & logic (use C operators):
  addi  rA,rB,N   →  rA = rB + N
  addis rA,rB,N   →  rA = rB + (N << 16)   /* high 16-bit immediate */
  ori   rA,rB,N   →  rA = rB | N
  oris  rA,rB,N   →  rA = rB | (N << 16)
  xori  rA,rB,N   →  rA = rB ^ N
  andi. rA,rB,N   →  rA = rB & N
  neg   rA,rB     →  rA = -rB
  mulli rA,rB,N   →  rA = rB * N
  divw  rA,rB,rC  →  rA = (s32)rB / (s32)rC
  divwu rA,rB,rC  →  rA = (u32)rB / (u32)rC

SPR / link register (compiler-managed — NEVER emit these in C):
  mflr  rA        →  (compiler saves LR; do not reproduce in C)
  mtlr  rA        →  (compiler restores LR; do not reproduce in C)
  mfspr/mtspr     →  hardware register access; never write in C
  blr             →  return   (do NOT write blr() — just use return)
  b  fn           →  tail call → return fn(...)   (branch without bl = tail call)
""")

# ─── Model history ────────────────────────────────────────────────────────────

def load_history() -> dict:
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {"units": {}, "stats": {}}


def save_history(history: dict):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def record_result(unit_name: str, model_id: str, model_level: int,
                  matched: bool, match_pct: float, asm_size: int, func_count: int):
    history = load_history()
    history["units"][unit_name] = {
        "model_id": model_id,
        "model_level": model_level,
        "matched": matched,
        "match_pct": match_pct,
        "asm_size": asm_size,
        "func_count": func_count,
        "timestamp": time.time(),
    }
    # Update per-model stats
    key = f"{model_id}_matched" if matched else f"{model_id}_failed"
    stats = history.setdefault("stats", {})
    stats[key] = stats.get(key, 0) + 1
    # Track size buckets for smart starting-model selection
    bucket = _size_bucket(asm_size)
    bucket_key = f"bucket_{bucket}_min_winning_level"
    current = stats.get(bucket_key, 999)
    if matched:
        stats[bucket_key] = min(current, model_level)
    save_history(history)


def _size_bucket(asm_size: int) -> str:
    """Bucket assembly byte size into broad complexity tiers."""
    if asm_size < 64:
        return "tiny"
    if asm_size < 256:
        return "small"
    if asm_size < 1024:
        return "medium"
    return "large"


def smart_start_level(asm_size: int, func_count: int) -> int:
    """
    Pick the lowest model level that has historically matched similar functions.
    Falls back to 0 (Qwen) if no history exists.
    """
    history = load_history()
    stats = history.get("stats", {})
    bucket = _size_bucket(asm_size)
    level = stats.get(f"bucket_{bucket}_min_winning_level", 0)
    # Never auto-start above level 3 — Claude slots (4+) may be paid
    # Level 0 = 7B (1060), Level 1 = 14B Q3 (5060 Ti), Level 2 = Devstral 24B (dual-GPU)
    # Level 3 = Qwen3-Coder 30B A3B (dual-GPU MoE)
    return min(level, 3)


# ─── Unit / source-file resolution ───────────────────────────────────────────

def _build_unit_map() -> dict:
    """Map unit_stem → source_path using objdiff.json (handles subdirs + .cpp)."""
    objdiff_json = PROJECT_ROOT / "objdiff.json"
    mapping = {}
    if not objdiff_json.exists():
        return mapping
    with open(objdiff_json) as f:
        data = json.load(f)
    for unit in data.get("units", []):
        full_name = unit.get("name", "")
        src_path  = unit.get("metadata", {}).get("source_path", "")
        if full_name and src_path:
            stem = full_name.split("/")[-1]
            mapping[stem] = PROJECT_ROOT / src_path
    return mapping


_UNIT_MAP = None

def _unit_map() -> dict:
    global _UNIT_MAP
    if _UNIT_MAP is None:
        _UNIT_MAP = _build_unit_map()
    return _UNIT_MAP


def resolve_unit(arg: str) -> str:
    arg = arg.strip()
    if re.match(r"^auto_", arg):
        return re.sub(r"\.(c|cpp)$", "", arg)
    addr = arg.upper().lstrip("0x")
    for stem in _unit_map():
        if addr in stem.upper():
            return stem
    # Fallback glob
    for ext in (".c", ".cpp"):
        matches = list(SRC_DIR.rglob(f"*{addr}*{ext}"))
        if matches:
            text = [m for m in matches if "_text" in m.name]
            return (text or matches)[0].stem
    raise ValueError(f"No source file found for '{arg}' — check src/ for *{arg.upper()}*")


def source_path_for(unit_name: str) -> Path:
    m = _unit_map()
    if unit_name in m:
        return m[unit_name]
    for ext in (".c", ".cpp"):
        hits = list(SRC_DIR.rglob(f"{unit_name}{ext}"))
        if hits:
            return hits[0]
    return SRC_DIR / f"{unit_name}.c"


# ─── File I/O ─────────────────────────────────────────────────────────────────

def load_assembly(unit_name: str) -> str:
    asm_file = ASM_DIR / f"{unit_name}.s"
    if not asm_file.exists():
        raise FileNotFoundError(f"Assembly not found: {asm_file}")
    return asm_file.read_text()


def load_current_source(unit_name: str) -> str:
    p = source_path_for(unit_name)
    return p.read_text() if p.exists() else ""


def _extract_c_code(response: str) -> str:
    """
    Strip prose and markdown from an LLM response, returning only C/C++ code.
    Priority:
      1. Content inside the LAST ```c … ``` or ``` … ``` fence (models often
         put explanation before the block, so last block = the final answer).
      2. Everything from the first #include / extern / static / void / int
         at the start of a line onwards — handles responses where the model
         dumps code without fences but prefixes it with a sentence.
      3. The raw response stripped of fence markers (fallback).
    """
    # 1. Fenced code block — prefer last one (most complete final answer)
    fences = list(re.finditer(
        r'```(?:c(?:\+\+)?|cpp)?\s*\n(.*?)```',
        response, re.DOTALL | re.IGNORECASE,
    ))
    if fences:
        return fences[-1].group(1).strip()

    # 2. Find first line that looks like C code
    c_start = re.search(
        r'^(#\s*(?:include|define|pragma|ifndef|ifdef|endif)|'
        r'(?:extern|static|inline|void|int|char|float|double|unsigned|signed|'
        r'u8|u16|u32|s8|s16|s32|f32|f64)\b)',
        response, re.MULTILINE,
    )
    if c_start:
        return response[c_start.start():].strip()

    # 3. Fallback: strip fence markers only
    code = re.sub(r"^```[a-z+]*\s*\n?", "", response, flags=re.MULTILINE)
    code = re.sub(r"^```\s*$",           "", code,     flags=re.MULTILINE)
    return code.strip()


def write_source(unit_name: str, c_code: str):
    c_code = _extract_c_code(c_code)
    # Strip // comments — project uses -lang=c (C89), which only allows /* */
    c_code = re.sub(r'(?<!:)//[^\n]*', '', c_code)
    c_code = c_code.strip() + "\n"
    p = source_path_for(unit_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(c_code)
    print(f"  ✎  → {p.relative_to(PROJECT_ROOT)}")


# ─── Context loading ──────────────────────────────────────────────────────────

def load_context(unit_name: str, asm_text: str) -> str:
    """
    Assemble decompilation context in relevance-first order.

    Hierarchy (most specific/confident -> least):
      T1  GDB runtime trace          -- live register/memory for THIS exact function
      T2  Cross-game symbol matches  -- ACGC-first (same AC lineage), then EGG/Brawl/etc.
      T3  SDK/framework headers      -- only headers whose patterns appear in THIS asm
      T4  High-confidence (>=70%) inferred callee names
      T5  AC class names + DWC structs (pattern-triggered)
      T6  ACCF symbols.txt refs + gecko codes matching this address
      T7  Medium-confidence (40-70%) inferred callee names
      T8  EGG struct layouts         -- only when EGG patterns detected
      T9  Item table                 -- only when item patterns detected

    A per-tier char budget ensures that low-priority bulk context never
    crowds out the high-value targeted data.
    """
    refs = set(re.findall(r"\b(fn_[0-9A-Fa-f]+|lbl_[0-9A-Fa-f]+)\b", asm_text))

    # -- T1: GDB runtime trace --------------------------------------------
    t1 = []
    gdb_ctx = _load_gdb_context(unit_name)
    if gdb_ctx:
        t1.append(gdb_ctx)

    # -- T2: Cross-game symbol matches (ACGC priority first) --------------
    t2 = []
    _rs_mod = None
    try:
        _rs_spec = importlib.util.spec_from_file_location(
            "resource_scraper", PROJECT_ROOT / "tools" / "resource_scraper.py")
        _rs_mod = importlib.util.module_from_spec(_rs_spec)
        _rs_spec.loader.exec_module(_rs_mod)
        cross_ctx = _rs_mod.build_cross_game_context(unit_name, asm_text)
        if cross_ctx:
            t2.append(cross_ctx)
    except Exception:
        pass

    # -- T3: SDK headers triggered by THIS function's asm patterns --------
    t3 = []
    if _rs_mod is not None:
        try:
            headers_ctx = _rs_mod.get_relevant_headers(unit_name, asm_text)
            if headers_ctx:
                t3.append(headers_ctx)
        except Exception:
            pass

    # -- T4+T7: Symbol name hints split by confidence ---------------------
    t4 = []
    t7_accum = []
    hints_file = PROJECT_ROOT / "data" / "symbol_hints.json"
    if hints_file.exists() and refs:
        try:
            with open(hints_file) as f:
                hints = json.load(f)
            fn_refs = [r for r in refs if r.startswith("fn_")]
            for ref in fn_refs:
                addr_m = re.search(r"fn_([0-9A-Fa-f]+)", ref, re.IGNORECASE)
                if not addr_m:
                    continue
                key  = f"fn_{addr_m.group(1).upper()}"
                info = hints.get(key)
                if not info:
                    continue
                conf = info.get("confidence", 0)
                entry = (f"  {ref} -> probable name: {info['name']} "
                         f"(confidence {conf:.0%}, source: {info['source']})")
                if conf >= 0.7:
                    t4.append(entry)
                elif conf >= 0.4:
                    t7_accum.append(entry)
        except Exception:
            pass
    if t4:
        t4 = ["=== High-confidence callee names (>=70%) ===\n" + "\n".join(t4[:20])]

    # -- T5: AC class names + DWC structs (pattern-triggered) -------------
    t5 = []
    ac_ctx = _load_ac_class_context(unit_name, asm_text)
    if ac_ctx:
        t5.append(ac_ctx)
    dwc_ctx = _load_dwc_context(unit_name, asm_text)
    if dwc_ctx:
        t5.append(dwc_ctx)

    # -- T6: ACCF symbols.txt references + gecko codes --------------------
    t6 = []
    if refs and SYMBOLS_FILE.exists():
        relevant = [
            line for line in SYMBOLS_FILE.read_text().splitlines()
            if any(r in line for r in refs)
        ]
        if relevant:
            t6.append("=== Referenced symbols (ACCF) ===\n" + "\n".join(relevant[:80]))
    addr_match = re.search(r"_([0-9A-Fa-f]{8})_", unit_name)
    if addr_match:
        addr = addr_match.group(1).upper()
        gecko_file = DATA_DIR / "gecko_codes.txt"
        if gecko_file.exists():
            gecko_lines = [
                l for l in gecko_file.read_text().splitlines()
                if addr[:4] in l.upper()
            ]
            if gecko_lines:
                t6.append("=== Matching gecko codes ===\n" + "\n".join(gecko_lines[:20]))

    # -- T7: Medium-confidence callee names (40-70%) ----------------------
    t7 = []
    if t7_accum:
        t7 = ["=== Callee name hints (40-70% confidence) ===\n" + "\n".join(t7_accum[:20])]

    # -- T8: EGG struct layouts (only when EGG patterns detected) ---------
    t8 = []
    if _asm_uses_egg(asm_text):
        t8.append(_EGG_STRUCT_CONTEXT)

    # -- T9: Item table (only when item patterns detected) ----------------
    t9 = []
    item_file = DATA_DIR / "item_table.json"
    if item_file.exists() and any(x in asm_text for x in ("0x9", "0xA", "0xB", "item", "Item")):
        try:
            with open(item_file) as f:
                items = json.load(f)
            sample = "\n".join(f"  0x{k}: {v}" for k, v in list(items.items())[:60])
            t9.append("=== Item IDs (hex -> name) ===\n" + sample)
        except Exception:
            pass

    # -- Assemble with per-tier char budget --------------------------------
    # Lower-priority tiers are truncated or dropped when total budget is full.
    TIER_CAPS = [
        (t1, 8000),   # T1 GDB trace          -- always include in full
        (t2, 4000),   # T2 cross-game symbols  -- generous: strongest naming hints
        (t3, 3000),   # T3 SDK headers
        (t4, 1500),   # T4 high-conf callee names
        (t5, 2000),   # T5 AC/DWC context
        (t6, 2000),   # T6 ACCF symbols + gecko
        (t7, 1000),   # T7 medium-conf callee names
        (t8, 1500),   # T8 EGG structs
        (t9,  800),   # T9 item table
    ]
    TOTAL_BUDGET = 22000   # ~5.5k tokens
    used  = 0
    parts = []
    for tier_list, cap in TIER_CAPS:
        if used >= TOTAL_BUDGET:
            break
        for piece in tier_list:
            if used >= TOTAL_BUDGET:
                break
            trimmed = piece[:cap]
            if trimmed:
                parts.append(trimmed)
                used += len(trimmed)

    return "\n\n".join(parts)

def _load_gdb_context(unit_name: str) -> str | None:
    """
    If Dolphin's GDB stub is reachable, probe the function at the address
    encoded in unit_name and return a runtime trace for the LLM.
    Silently returns None if Dolphin is not running or probe is disabled.
    """
    addr_m = re.search(r"_([0-9A-Fa-f]{8})_", unit_name)
    if not addr_m:
        return None

    try:
        from tools.gdb_probe import dolphin_stub_reachable, probe_function  # type: ignore
    except ImportError:
        try:
            import importlib.util, sys as _sys
            spec = importlib.util.spec_from_file_location(
                "gdb_probe", PROJECT_ROOT / "tools" / "gdb_probe.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            dolphin_stub_reachable = mod.dolphin_stub_reachable
            probe_function         = mod.probe_function
        except Exception:
            return None

    if not dolphin_stub_reachable():
        return None

    addr = int(addr_m.group(1), 16)
    print(f"  🎮  Dolphin GDB stub detected — probing 0x{addr:08X} …", flush=True)
    return probe_function(addr, hits=1)


# ── EGG framework struct definitions ─────────────────────────────────────────
# Source: https://github.com/vabold/EGG  (pre-2011 layout, ACCF = Nov 2008)
_EGG_STRUCT_CONTEXT = """\
=== EGG framework struct layouts (ACCF uses EGG pre-2011) ===
/* nw4r::ut::Link — intrusive linked-list node, 0x08 bytes */
typedef struct {
    void *next;   /* +0x00 */
    void *prev;   /* +0x04 */
} nw4r_ut_Link;

/* nw4r::ut::List — list head, 0x0C bytes */
typedef struct {
    void     *head;   /* +0x00 */
    void     *tail;   /* +0x04 */
    u16       count;  /* +0x08 */
    u16       offset; /* +0x0A */
} nw4r_ut_List;

/* EGG::Disposer — base for heap-tracked objects, 0x10 bytes */
typedef struct EGG_Disposer {
    void            *vtable;  /* +0x00 (virtual ~Disposer) */
    void            *mHeap;   /* +0x04 — pointer to owning EGG::Heap */
    nw4r_ut_Link     mLink;   /* +0x08 — node in Heap::mChildren list */
} EGG_Disposer; /* sizeof == 0x10 */

/* EGG::Heap — base heap class, 0x38 bytes (ACCF/pre-2011 build) */
typedef struct EGG_Heap {
    void            *vtable;    /* +0x00 */
    u8               _04[0x24]; /* +0x04 — MEM heap handle + flags */
    nw4r_ut_List     mChildren; /* +0x28 — list of attached Disposers */
    u8               _34[0x04]; /* +0x34 — padding */
} EGG_Heap; /* sizeof == 0x38 */

/* EGG::Fader — base class for screen faders (source: vabold/bba-wd) */
typedef enum {
    FADER_STATUS_OPAQUE   = 0,
    FADER_STATUS_HIDDEN   = 1,
    FADER_STATUS_FADE_IN  = 2,
    FADER_STATUS_FADE_OUT = 3,
} EFaderStatus;

/* EGG::ColorFader : public EGG::Fader
   Constructor: ColorFader(f32 left, f32 top, f32 width, f32 height,
                            nw4r::ut::Color color, EStatus status)
   Default frame count = 20 */
typedef struct EGG_ColorFader {
    void          *vtable;      /* +0x00 */
    EFaderStatus   mStatus;     /* +0x04 — current fade state */
    u8             mFlags;      /* +0x08 — bit0=notify fadeIn done, bit1=notify fadeOut done */
    u16            mFrame;      /* +0x0A — total frames for fade (default 20) */
    u16            mFrameTimer; /* +0x0C — current frame counter */
    /* nw4r::ut::Color mColor — RGBA, each u8 */
    u8             mColor_r;    /* +0x0E */
    u8             mColor_g;    /* +0x0F */
    u8             mColor_b;    /* +0x10 */
    u8             mColor_a;    /* +0x11 — driven by calc() */
    /* nw4r::ut::Rect mRect — f32 left/top/right/bottom */
    f32            mRect_left;  /* +0x14 */
    f32            mRect_top;   /* +0x18 */
    f32            mRect_right; /* +0x1C */
    f32            mRect_bottom;/* +0x20 */
} EGG_ColorFader; /* sizeof ≈ 0x24 */
/* vtable layout (Fader virtual):
   +0x00 setStatus(EStatus)
   +0x04 getStatus() const
   +0x08 fadeIn()   → returns true if status was OPAQUE (success)
   +0x0C fadeOut()  → returns true if status was HIDDEN (success)
   +0x10 calc()     → advance timer, update mColor.a, returns true when done
   +0x14 draw()     → GX quad with mColor.a blending */"""


def _asm_uses_egg(asm_text: str) -> bool:
    """Return True if the assembly likely touches EGG types."""
    egg_indicators = [
        "EGG", "eggHeap", "eggDisposer", "eggArchive",
        "eggExpHeap", "eggFrmHeap", "mChildren", "mHeap",
        "ColorFader", "eggColorFader", "eggFader",
        "FADER_STATUS", "mFrameTimer", "mFrame",
        # common EGG heap vtable stubs appear near these addresses
        "0x10(r", "0x28(r", "0x08(r",   # Disposer/Heap field offsets
    ]
    return any(ind in asm_text for ind in egg_indicators)


# Cache the AC class context so we don't re-read the file on every call.
_AC_CLASS_CONTEXT_CACHE: str | None = None

_AC_UNIT_PREFIXES = ("Ac", "Bs", "Npc", "Ftr", "Fld", "Bg", "Gm", "Ui",
                     "Fs", "Mg", "Mu", "Rs", "ColorFader", "Fade", "Menu",
                     "Layout", "Scene", "Actor", "Base")

def _load_ac_class_context(unit_name: str, asm_text: str) -> str:
    """
    Return the AC class name context block if this unit looks like AC game code.
    Uses data/ac_class_context.txt built by cross_game_symbols.py.
    """
    global _AC_CLASS_CONTEXT_CACHE

    ctx_file = PROJECT_ROOT / "data" / "ac_class_context.txt"
    if not ctx_file.exists():
        return ""

    # Only inject when the unit / asm looks like AC game code (not pure SDK)
    is_ac_unit = any(p in unit_name for p in _AC_UNIT_PREFIXES)
    has_ac_asm = any(p in asm_text for p in ("AcNpc", "AcFtr", "AcStrc", "BsScene",
                                              "BsMgr", "NpcModel", "ColorFader"))
    if not (is_ac_unit or has_ac_asm):
        return ""

    if _AC_CLASS_CONTEXT_CACHE is None:
        _AC_CLASS_CONTEXT_CACHE = ctx_file.read_text()

    return "=== Known Animal Crossing class names (for context) ===\n" + _AC_CLASS_CONTEXT_CACHE


# ── DWC (Nintendo Wi-Fi Connection) SDK context ───────────────────────────────
# Source: OpenPayload (CLF78/OpenPayload), doldecomp/mkw
# The same DWC SDK is linked into ACCF for online/WFC features.

_DWC_CONTEXT_CACHE: str | None = None

_DWC_UNIT_PREFIXES = ("Wifi", "WFC", "wifi", "wfc", "DWC", "dwc", "Online",
                      "online", "Friend", "NWC24", "nwc24", "Match", "NATNEG",
                      "GT2", "GS", "qr2", "Net", "Network")
_DWC_ASM_TRIGGERS  = ("DWC_", "DWCi_", "NWC24", "udp_cc", "DWC_Error",
                      "NATNEG", "natneg", "gt2", "qr2_", "GameSpy",
                      "nintendowifi", "gpcm", "gpsp", "naswii")


def _load_dwc_context(unit_name: str, asm_text: str) -> str:
    """Return DWC SDK struct context when the unit touches WFC/online code."""
    global _DWC_CONTEXT_CACHE

    ctx_file = PROJECT_ROOT / "data" / "dwc_context.txt"
    if not ctx_file.exists():
        return ""

    is_dwc_unit = any(p in unit_name for p in _DWC_UNIT_PREFIXES)
    has_dwc_asm = any(p in asm_text for p in _DWC_ASM_TRIGGERS)
    if not (is_dwc_unit or has_dwc_asm):
        return ""

    if _DWC_CONTEXT_CACHE is None:
        _DWC_CONTEXT_CACHE = ctx_file.read_text()

    return "=== DWC (Nintendo Wi-Fi Connection) SDK types ===\n" + _DWC_CONTEXT_CACHE


# ─── Prompt builder ───────────────────────────────────────────────────────────

def build_prompt(unit_name: str, asm: str, ctx: str,
                 prev_c: str, prev_match: float, attempt: int,
                 diff_text: str = "",
                 fn_status: list[dict] | None = None,
                 compile_error: str = "",
                 ghidra_ctx: str = "") -> str:
    sections = []

    # Referenced symbols / item table / gecko codes
    if ctx:
        sections.append(ctx)

    # Ghidra pseudo-C — structural reference to help with first attempts
    if ghidra_ctx and attempt == 0:
        sections.append(ghidra_ctx)

    sections.append(f"=== Target assembly ({unit_name}.s) — reproduce exactly ===\n{asm}")

    if attempt == 0 or not prev_c:
        sections.append(textwrap.dedent("""\
            Decompile the assembly above into C/C++ that compiles to a byte-for-byte match.

            Work through this in order:
            1. Count arguments: which registers (r3-r10) are read before being written at
               function entry? Those are the parameters.
            2. Identify return type: what is in r3 just before every `blr`?
            3. Map struct offsets: each `lwz rN, 0xXX(rM)` / `stw` reveals a field at that
               exact byte offset — use the same offset in your C struct/cast, not a guess.
            4. Identify tail calls: a bare `b fn_XXXX` (not `bl`) is `return fn_XXXX(...)`.
            5. Write the C code. Output ONLY the source file, nothing else."""))
    else:
        if compile_error:
            retry = (
                f"=== Previous attempt FAILED TO COMPILE ===\n{prev_c}\n\n"
                f"Compiler error:\n{compile_error}\n\n"
                "Fix the compiler error. Do NOT change any logic that was already correct.\n"
            )
        else:
            retry = (
                f"=== Previous attempt scored {prev_match:.1f}% — need 100% ===\n{prev_c}\n\n"
            )

        if fn_status:
            ok     = [f["name"] for f in fn_status if (f.get("fuzzy_match_percent") or 0) >= 100.0]
            broken = [f for f in fn_status if (f.get("fuzzy_match_percent") or 0) < 100.0]
            if ok:
                retry += f"ALREADY CORRECT — do NOT touch: {', '.join(ok)}\n"
            if broken:
                pcts = ", ".join(
                    f"{f['name']} ({f.get('fuzzy_match_percent', 0):.1f}%)"
                    for f in broken
                )
                retry += f"NEEDS FIXING: {pcts}\n"
            retry += "\n"

        if diff_text:
            retry += (
                "=== Instruction diff  (TARGET=what we need | YOURS=what you produced) ===\n"
                "  Arguments wrapped in {braces} are the ones that differ.\n"
                + diff_text + "\n\n"
                "Fix ONLY the diffing instructions. Precise checklist:\n"
                "  • {reg} mismatch → wrong variable/expression producing that register\n"
                "  • {0xNN} offset mismatch → struct field offset or array stride is wrong\n"
                "  • OP_MISMATCH (different opcode) → wrong operator or signedness\n"
                "  • srwi = unsigned >>,  srawi = signed >> — do NOT mix them\n"
                "  • cmplw = unsigned compare,  cmpw = signed compare\n"
                "  • bare `b fn_X` (no `bl`) = tail call → `return fn_X(...)`\n"
                "  • `li rN, -1` encodes as 0xFFFF — check sign\n"
                "  • Every lwz/stw offset must match exactly — no rounding\n"
            )
        else:
            retry += (
                "No diff available. Analyse the assembly from scratch and rewrite.\n"
                "Pay special attention to:\n"
                "  • Argument count and types (r3-r10 at entry)\n"
                "  • Struct field offsets (must match each lwz/stw exactly)\n"
                "  • Signed vs unsigned types throughout\n"
            )
        sections.append(retry)

    return "\n\n".join(sections)


# ─── LLM callers ──────────────────────────────────────────────────────────────

def unload_model(m: dict) -> None:
    """
    Explicitly evict a model from VRAM by sending keep_alive=0.
    Called before loading a heavy dual-GPU tier so lower-tier models
    release the 1060's VRAM, making it available for the heavy model to span.
    Silently ignores any error — the heavy call will simply get less VRAM if it fails.
    """
    if m.get("backend") != "ollama":
        return
    target = m.get("url", OLLAMA_URL)
    try:
        import httpx
        httpx.post(target, json={"model": m["name"], "keep_alive": 0}, timeout=10)
        print(f"  🗑   Unloaded {m['label'].strip()} from VRAM", flush=True)
    except Exception:
        pass


def call_ollama(model_name: str, prompt: str, timeout: int = 300,
                url: str | None = None, system: str | None = None) -> str:
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx not installed — pip install httpx")

    target_url = url or OLLAMA_URL
    payload = {
        "model": model_name,
        "prompt": (system or SYSTEM_PROMPT) + "\n\n" + prompt,
        "stream": True,
        "keep_alive": -1,  # keep model resident in VRAM between calls
        "options": {"temperature": 0.0, "num_predict": 4096},
    }
    tokens = []
    print("  ", end="", flush=True)
    with httpx.stream("POST", target_url, json=payload, timeout=timeout) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            token = chunk.get("response", "")
            tokens.append(token)
            # Print a dot every ~50 tokens so the terminal shows progress
            if len(tokens) % 50 == 0:
                print(".", end="", flush=True)
            if chunk.get("done"):
                break
    print()  # newline after dots
    return "".join(tokens).strip()


def call_claude(model_name: str, prompt: str) -> str:
    try:
        from anthropic import Anthropic
    except ImportError:
        raise RuntimeError("anthropic not installed — pip install anthropic")

    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or "unused"
        print(f"  🔀  Using base URL override: {base_url}", flush=True)
    else:
        key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("Set CLAUDE_API_KEY or ANTHROPIC_API_KEY env var")

    # Enforce spend cap (skip when using free proxy)
    if not base_url and _session_spend >= CLAUDE_SPEND_CAP_USD:
        raise RuntimeError(
            f"Session Claude spend cap ${CLAUDE_SPEND_CAP_USD:.2f} reached "
            f"(${_session_spend:.4f} so far). Set CLAUDE_SPEND_CAP env var to raise it."
        )

    client_kwargs = {"api_key": key}
    if base_url:
        client_kwargs["base_url"] = base_url

    client = Anthropic(**client_kwargs)
    chunks = []
    char_count = 0
    final_msg = None
    with client.messages.stream(
        model=model_name,
        max_tokens=8096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)
            char_count += len(text)
            # Print a dot every ~200 chars so terminal shows progress
            if char_count // 200 > (char_count - len(text)) // 200:
                print(".", end="", flush=True)
        try:
            final_msg = stream.get_final_message()
        except Exception:
            pass

    result = "".join(chunks).strip()

    # Track cost using actual token counts from the API response (skip for free proxy)
    if not base_url:
        if final_msg and hasattr(final_msg, "usage"):
            u = final_msg.usage
            _record_claude_cost(model_name,
                                getattr(u, "input_tokens", 0),
                                getattr(u, "output_tokens", 0))
        else:
            # Fallback estimate: 1 token ≈ 4 chars
            _record_claude_cost(model_name,
                                len(SYSTEM_PROMPT + prompt) // 4,
                                len(result) // 4)
    else:
        print(f"  🆓  Free proxy — no cost tracked", flush=True)

    # Warn loudly if the response is suspiciously short — means something went wrong
    if len(result) < 80:
        print(f"\n  ⚠  Claude returned almost nothing ({len(result)} chars): {repr(result[:200])}")
        raise RuntimeError(
            f"Claude response too short ({len(result)} chars) — "
            "possible refusal or model error. Not retrying with paid model."
        )

    return result


def generate_c(level: int, prompt: str,
               url_override: str | None = None) -> tuple[str, int]:
    """
    Generate C code using the model at the given level.
    Returns (c_code, actual_level_used).
    Raises on hard failure (no more models to try).

    url_override: force a specific Ollama port (used by parallel per-fn workers
                  to route different functions to different GPU instances).

    GPU memory management (Windows dual-GPU profile):
      Tiers 0–1 (7B, 14B Q3) stay resident in VRAM between calls (keep_alive=-1).
      Before tier 2+ (Devstral, Qwen3 30B) loads, all lower-tier Ollama models are
      evicted so the heavy model can span both GPUs cleanly:
        GPU 0 (RTX 5060 Ti 8GB GDDR7) + GPU 1 (GTX 1060 6GB GDDR5) = 14 GB
        Devstral 24B ~15 GB → ~8+6+1 GB (1 GB CPU spill, acceptable)
        Qwen3 30B A3B Q4 ~19 GB → ~8+6+5 GB (5 GB CPU spill)
    """
    if level >= len(MODELS):
        raise RuntimeError("Exhausted all models")

    m = MODELS[level]

    # ── Heavy-tier VRAM prep ───────────────────────────────────────────────────
    # Tier 2+ (Devstral / Qwen3) need both GPUs. Evict lower-tier models first.
    # Skip eviction when url_override is set — we're in parallel mode, the caller
    # explicitly chose which port/model to use.
    if _PROFILE == "windows" and level >= 2 and not url_override:
        for lower in MODELS[:level]:
            unload_model(lower)

    # Parallel workers may override the URL to target a specific GPU instance
    effective_url = url_override or m.get("url")

    icon = "🤖" if m["backend"] == "ollama" else "✨"
    port_tag = f":{effective_url.split(':')[2].split('/')[0]}" if effective_url else ""
    print(f"  {icon}  [{m['label'].strip()}{port_tag}] …", end="", flush=True)
    t0 = time.time()

    # Skip Claude entirely if a previous call already returned 401/403
    global _CLAUDE_UNAVAILABLE
    if m["backend"] == "claude" and _CLAUDE_UNAVAILABLE:
        print(f"  ⏭   Skipping {m['label'].strip()} (API unavailable this session)")
        return generate_c(level + 1, prompt)

    # Heavy Ollama tiers (Devstral, Qwen3 30B) get a longer timeout
    _ollama_timeout = 600 if level >= 2 else 300

    try:
        if m["backend"] == "ollama":
            result = call_ollama(m["name"], prompt, timeout=_ollama_timeout, url=effective_url)
        else:
            result = call_claude(m["name"], prompt)
        elapsed = time.time() - t0
        print(f"  ({elapsed:.1f}s)")
        return result, level
    except Exception as e:
        elapsed = time.time() - t0
        err_str = str(e)
        # Detect auth failures and disable Claude for the rest of this session
        if m["backend"] == "claude" and ("401" in err_str or "403" in err_str or "Invalid API key" in err_str):
            _CLAUDE_UNAVAILABLE = True
            print(f"  !! CLAUDE API UNAVAILABLE — disabling for this session")
        else:
            print(f"  failed ({elapsed:.1f}s): {e}")
            print(f"  ⚠  {m['label'].strip()} failed — stepping up")
        # Step up automatically (no url_override on escalation — let model decide)
        return generate_c(level + 1, prompt)


# ─── Build & match ────────────────────────────────────────────────────────────

def _parse_mwcc_error(stderr: str) -> str:
    """
    Parse mwcceppc compiler output and return a concise error context string
    suitable for including in the next LLM prompt.

    mwcceppc error block looks like:
      ### mwcceppc.exe Compiler:
      #    File: src\\unit.c
      # -----------
      #       8: u16* ptr = (u16*)((u8*)&itemID + 2); // comment
      #   Error: ^^^
      #   expression syntax error
    """
    lines = stderr.splitlines()
    file_name = ""
    code_line = ""
    line_num  = ""
    errors    = []
    i = 0
    while i < len(lines):
        l = lines[i]
        # File
        m = re.search(r"File:\s*(.+)", l)
        if m:
            file_name = m.group(1).strip()
        # Code line with line number:  #    <N>: <code>
        m = re.match(r"#\s+(\d+):\s+(.*)", l)
        if m:
            line_num  = m.group(1)
            code_line = m.group(2).strip()
        # Error message (line after "Error: ^^^")
        if "Error:" in l and "^^^" in l and i + 1 < len(lines):
            msg = lines[i + 1].lstrip("#").strip()
            if msg:
                errors.append(msg)
        i += 1

    if not errors and not code_line:
        # Fallback: return last 15 lines
        return "\n".join(lines[-15:])

    parts = []
    if file_name:
        parts.append(f"File: {file_name}")
    if line_num and code_line:
        parts.append(f"Line {line_num}: {code_line}")
    if errors:
        parts.append(f"Error: {'; '.join(errors)}")
    return "\n".join(parts)

_BUILD_NINJA_REFRESHED = False   # run configure.py once per process


def _patch_build_ninja():
    """
    Patch build.ninja so ninja never tries to auto-regenerate it:
      1. Replace python= with sys.executable (correct interpreter).
      2. Strip the 'build build.ninja: configure' generator rule and the
         'rule configure' block entirely — we call configure.py ourselves,
         so we don't want ninja to ever attempt it via the configure rule
         (which fails in headless / CI environments).
    """
    ninja_file = PROJECT_ROOT / "build.ninja"
    if not ninja_file.exists():
        return
    text = ninja_file.read_text()

    # 1. Fix python interpreter
    _exe = sys.executable
    patched = re.sub(
        r'^python\s*=.*$', lambda m: f'python = {_exe}',
        text, flags=re.MULTILINE,
    )

    # 2. Strip the entire "# Reconfigure on change" section: comment + rule configure
    #    block + build build.ninja statement (including $ continuation lines).
    #    We match from the comment through the first blank line that follows.
    patched = re.sub(
        r'# Reconfigure on change\n'          # section header comment
        r'rule configure\n'                    # rule declaration
        r'(?:[ \t]+[^\n]*\n)*'                # indented rule body lines
        r'build build\.ninja[^\n]*\n'         # build statement (first line)
        r'(?:[ \t]+[^\n]*\n)*'               # continuation lines (indented)
        r'\n?',                               # trailing blank line
        '',
        patched,
        flags=re.MULTILINE,
    )

    if patched != text:
        ninja_file.write_text(patched)


def _ensure_build_ninja():
    """
    Run configure.py once per process (to reset timestamps), then patch
    python= and touch so ninja never tries to regenerate via system python.
    """
    global _BUILD_NINJA_REFRESHED
    ninja_file = PROJECT_ROOT / "build.ninja"

    if not _BUILD_NINJA_REFRESHED:
        r = subprocess.run([sys.executable, "configure.py"],
                           cwd=PROJECT_ROOT, capture_output=True, text=True)
        if r.returncode != 0 and not ninja_file.exists():
            print(f"  ⚠  configure.py failed:\n{r.stderr[:400]}")
            return
        _BUILD_NINJA_REFRESHED = True
        global _TARGET_PREFIX
        _TARGET_PREFIX = None   # re-probe after configure.py regenerates build.ninja
        _UNIT_PREFIX_CACHE.clear()

    _patch_build_ninja()


def build_unit(unit_name: str) -> tuple[bool, str]:
    """Returns (success, compiler_error_text)."""
    _ensure_build_ninja()
    target = f"build/RUUE01/{_get_target_prefix(unit_name)}/{unit_name}.o"
    print(f"  🔨  ninja {target}")
    r = subprocess.run(["ninja", target], cwd=PROJECT_ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        stderr = r.stderr.strip()
        if "rebuilding" in stderr and "build.ninja" in stderr:
            print("  🔄  build.ninja stale — re-running configure.py …")
            global _BUILD_NINJA_REFRESHED
            _BUILD_NINJA_REFRESHED = False   # force a fresh configure run
            _ensure_build_ninja()            # runs configure.py, patches python=, touches
            r = subprocess.run(["ninja", target], cwd=PROJECT_ROOT,
                               capture_output=True, text=True)
            if r.returncode == 0:
                return True, ""
            stderr = r.stderr.strip()
        lines = stderr.splitlines()
        for line in lines[-30:]:
            print(f"     {line}")
        # Parse mwcceppc error block: extract line number, code, and message
        # Format:  #    <N>: <code>
        #          #   Error: ^^^
        #          #   <message>
        err_context = _parse_mwcc_error(stderr)
        return False, err_context
    return True, ""


def regenerate_report() -> dict:
    try:
        r = subprocess.run(
            [str(OBJDIFF_CLI), "report", "generate", "-o", str(REPORT_JSON)],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  ⚠  objdiff-cli: {r.stderr[:200]}")
    except OSError as e:
        print(f"  ⚠  objdiff-cli not runnable ({e}); using cached report")
    if REPORT_JSON.exists():
        with open(REPORT_JSON) as f:
            return json.load(f)
    return {}


# ─── JSON diff rendering (adapted from PrimeDecomp/prime scripts/decomp-diff.py) ─

def _arg_value(arg: dict, inst: dict, all_syms: list) -> str:
    """Extract a human-readable value from a JSON instruction argument."""
    if "opaque" in arg:
        return str(arg["opaque"])
    if "signed" in arg:
        n = int(arg["signed"])
        return f"-0x{-n:x}" if n < -9 else (f"0x{n:x}" if n > 9 else str(n))
    if "unsigned" in arg:
        n = int(arg["unsigned"])
        return f"0x{n:x}" if n > 9 else str(n)
    if "branch_dest" in arg:
        return f"0x{int(arg['branch_dest']):x}"
    if "reloc" in arg:
        ts = inst.get("relocation", {}).get("target_symbol")
        if ts is not None and ts < len(all_syms):
            sym = all_syms[ts]
            return sym.get("demangled_name", sym.get("name", "?"))
        return "?"
    return str(arg)


def _render_instr(entry: dict, all_syms: list, mark_diffs: bool = False) -> str:
    """Render a single instruction entry from objdiff JSON to text."""
    inst      = entry.get("instruction", {})
    arg_diffs = entry.get("arg_diff", [])
    parts     = inst.get("parts", [])
    out       = []
    arg_idx   = 0
    for part in parts:
        if "opcode" in part:
            out.append(part["opcode"]["mnemonic"])
            out.append(" ")
        elif "arg" in part:
            val      = _arg_value(part["arg"], inst, all_syms)
            diffed   = (mark_diffs and arg_idx < len(arg_diffs)
                        and arg_diffs[arg_idx].get("diff_index") is not None)
            out.append(f"{{{val}}}" if diffed else val)
            arg_idx += 1
        elif "separator" in part:
            out.append(", ")
        elif "basic" in part:
            out.append(part["basic"])
    return "".join(out).rstrip()


def _render_json_diff(data: dict, context: int = 3, max_lines: int = 120) -> str:
    """
    Render structured per-function diffs from objdiff JSON output.
    Mismatching args are wrapped in {} so the LLM knows exactly what to fix.
    """
    left_syms  = data.get("left",  {}).get("symbols", [])
    right_syms = data.get("right", {}).get("symbols", [])
    output     = []

    for lsym in left_syms:
        if "instructions" not in lsym:
            continue
        mp   = lsym.get("match_percent")
        if mp is None or mp >= 100.0:
            continue
        name = lsym.get("demangled_name", lsym.get("name", "?"))
        size = int(lsym.get("size", 0))
        ts   = lsym.get("target_symbol")
        rsym = right_syms[ts] if ts is not None and ts < len(right_syms) else {}

        output.append(f"\n--- {name}  {mp:.1f}%  ({size}B) ---")
        output.append(f"  {'OFFSET':>6}  {'TARGET (want)':^38}  {'YOURS (got)':^38}")
        output.append(f"  {'-'*6}  {'-'*38}  {'-'*38}")

        linsts = lsym.get("instructions", [])
        rinsts = rsym.get("instructions", []) if rsym else []
        n      = max(len(linsts), len(rinsts))

        printed: set[int] = set()
        for i in range(n):
            li = linsts[i] if i < len(linsts) else {}
            ri = rinsts[i] if i < len(rinsts) else {}
            lk = li.get("diff_kind", "")
            rk = ri.get("diff_kind", "")
            if not (lk or rk):
                continue
            # print context window around mismatch
            for j in range(max(0, i - context), min(n, i + context + 1)):
                if j in printed:
                    continue
                printed.add(j)
                lj = linsts[j] if j < len(linsts) else {}
                rj = rinsts[j] if j < len(rinsts) else {}
                jk = lj.get("diff_kind", "") or rj.get("diff_kind", "")
                marker = "~" if jk else " "
                addr_raw = lj.get("instruction", {}).get("address") or \
                           rj.get("instruction", {}).get("address") or 0
                addr     = f"{int(addr_raw):x}"
                lt = _render_instr(lj, left_syms,  mark_diffs=True)  if lj else ""
                rt = _render_instr(rj, right_syms, mark_diffs=True)  if rj else ""
                output.append(f"  {marker}{addr:>6}  {lt:<38}  {rt:<38}")

        if len(output) >= max_lines:
            output.append(f"  … (truncated at {max_lines} lines)")
            break

    return "\n".join(output)


def get_objdiff_diff(unit_name: str) -> str:
    """
    Get an instruction-level diff for a unit using objdiff-cli JSON output.
    Mismatching arguments are wrapped in {} so the LLM knows exactly what to fix.
    Falls back to plain text diff if JSON is unavailable.
    """
    try:
        r = subprocess.run(
            [str(OBJDIFF_CLI), "diff",
             "-u", f"main/{unit_name}",
             "-o", "-", "--format", "json",
             "-c", "functionRelocDiffs=data_value"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
    except OSError:
        return ""
    if r.returncode == 0 and r.stdout.strip():
        try:
            return _render_json_diff(json.loads(r.stdout))
        except (json.JSONDecodeError, Exception):
            pass
    # Fallback: plain text
    r2 = subprocess.run(
        [str(OBJDIFF_CLI), "diff", "--unit", f"main/{unit_name}"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if r2.returncode != 0 or not r2.stdout.strip():
        return ""
    important = [l for l in r2.stdout.splitlines()
                 if any(m in l for m in ("<", ">", "|", "FAIL", "MISMATCH", "≠", "✗"))]
    lines = (important or r2.stdout.splitlines())[:120]
    return "\n".join(lines)


# ─── Pre-compile validation ───────────────────────────────────────────────────

# Patterns that are never valid in mwcceppc C89 and indicate hallucination
_BAD_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PPC register names used as C identifiers (assignment/index only — not in comments/ranges)
    # Uses [=\*\/\[] not [-\+] to avoid firing on ranges like r3-r10 in comments
    (re.compile(r'(?<!\w)(r\d{1,2}|f\d{1,2}|cr\d|lr|ctr|xer|sp)(?!\w)\s*[=\*\/\[]'),
     "PPC register name used as C variable (r0-r31, f0-f31, lr, ctr, etc.)"),
    # PPC intrinsics called as functions
    (re.compile(r'\b(stw|stwu|stwx|lwz|lwzx|lhz|lhzx|lha|lhax|lbz|lbzx|'
                r'sth|sthx|stb|stbx|stmw|lmw|lswi|stswi|'
                r'lfs|lfsx|lfd|lfdx|stfs|stfsx|stfd|stfdx|'
                r'blr|bctr|bclr|extrwi|rlwinm|rlwimi|rlwnm|'
                r'mflr|mtlr|mtspr|mfspr|srwi|slwi|srawi|'
                r'addi|addis|ori|oris|xori|andi|mulli|'
                r'divw|divwu|mullw|mulhw|mulhwu|'
                r'neg|nor|nand|eqv|andc|orc|'
                r'cntlzw|extsh|extsb)\s*\('),
     "PPC instruction used as a C function call — express as C operators/dereferences"),
    # C++ comments in C89 mode
    (re.compile(r'(?<![:\w])//(?!/)'),
     "C++ comment (//) not allowed in -lang=c mode — use /* */ instead"),
    # __asm__ blocks
    (re.compile(r'\b__asm__\s*\('),
     "__asm__ is a GCC extension, not valid in mwcceppc"),
    # long long / int64
    (re.compile(r'\blong\s+long\b|\bint64_t\b|\buint64_t\b'),
     "long long / int64_t not available — ptrdiff_t is 32-bit on this target"),
]


def _prevalidate(c_code: str) -> str:
    """
    Scan generated C code for known-bad patterns before sending it to the compiler.
    Returns a human-readable error string, or '' if the code looks clean.
    Strips comments first so patterns inside /* ... */ never fire.
    """
    # Strip block comments so we only check actual code
    stripped = re.sub(r'/\*.*?\*/', '', c_code, flags=re.DOTALL)
    for pat, description in _BAD_PATTERNS:
        m = pat.search(stripped)
        if m:
            line_no = stripped[:m.start()].count('\n') + 1
            snippet = stripped[max(0, m.start()-30):m.end()+30].strip().splitlines()[0]
            return f"Line ~{line_no}: {description}\n  Snippet: {snippet}"
    return ""


# ─── Ghidra context (best-effort, won't block if unavailable) ─────────────────

_GHIDRA_URL: str | None = None   # cached; False = confirmed unavailable


def _ghidra_base() -> str | None:
    """
    Probe known decompiler server addresses and return the URL with the MOST
    functions loaded.  IDA (8081) is preferred when both servers have a
    comparable count (>= 80% of the best), otherwise use whichever has more.
    Servers that respond on /ping but not /list_functions are scored at 1.
    A server with 0 functions is skipped entirely.
    """
    global _GHIDRA_URL
    if _GHIDRA_URL is not None:
        return _GHIDRA_URL or None
    try:
        import httpx
        candidates: list[tuple[int, str]] = []   # (function_count, url)
        for url in ("http://localhost:8081",   # IDA Pro Hex-Rays
                    "http://localhost:8080",   # Ghidra
                    "http://localhost:18001",
                    "http://localhost:9090",
                    "http://localhost:8765"):
            try:
                r = httpx.get(f"{url}/list_functions", timeout=2.0)
                if r.status_code == 200:
                    funcs = r.json()
                    if isinstance(funcs, list) and len(funcs) > 0:
                        candidates.append((len(funcs), url))
                    continue   # skip 0-function servers
            except Exception:
                pass
            # /list_functions not supported — try /ping as last resort (score=1)
            try:
                r = httpx.get(f"{url}/ping", timeout=1.5)
                if r.status_code < 500:
                    candidates.append((1, url))
            except Exception:
                pass

        if not candidates:
            _GHIDRA_URL = False   # type: ignore[assignment]
            return None

        best_count, best_url = max(candidates, key=lambda x: x[0])

        # If IDA (8081) is within 80% of the best, prefer it for Hex-Rays quality.
        ida_entry = next((c for c in candidates if "8081" in c[1]), None)
        if ida_entry and ida_entry[0] >= best_count * 0.8:
            _GHIDRA_URL = ida_entry[1]
        else:
            _GHIDRA_URL = best_url

        return _GHIDRA_URL
    except ImportError:
        pass
    _GHIDRA_URL = False   # type: ignore[assignment]
    return None


def _ghidra_decompile(addr_hex: str) -> str:
    """
    Ask the Ghidra MCP server to decompile one function.
    Returns pseudo-C string or '' if not available.
    Our ghidra_server.py exposes GET /decompile?address=0x... or ?name=...
    """
    base = _ghidra_base()
    if not base:
        return ""
    try:
        import httpx
        # Try by address first (our server accepts hex string with or without 0x)
        for addr_param in (f"0x{addr_hex}", addr_hex):
            try:
                r = httpx.get(f"{base}/decompile",
                              params={"address": addr_param}, timeout=15.0)
                if r.status_code == 200:
                    data = r.json()
                    for key in ("decompiled", "code", "result", "c_code"):
                        val = data.get(key, "")
                        if val and len(val) > 40:
                            return val
            except Exception:
                continue
    except Exception:
        pass
    return ""


def load_ghidra_context(unit_name: str, asm_text: str) -> str:
    """
    Pull Ghidra pseudo-C for the unit's functions (up to 3).
    Used as a reference in the prompt — gives the LLM a structural head-start.
    """
    addrs = re.findall(r"^\.fn\s+fn_([0-9A-Fa-f]{8})", asm_text, re.MULTILINE)
    if not addrs:
        m = re.search(r"_([0-9A-Fa-f]{8})_", unit_name)
        if m:
            addrs = [m.group(1)]
    if not addrs:
        return ""

    parts = []
    for addr in addrs[:3]:
        code = _ghidra_decompile(addr)
        if code:
            # Strip Ghidra noise comments, keep just the function body
            code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL).strip()
            if len(code) > 80:
                parts.append(f"/* Ghidra pseudo-C for fn_{addr} — use as structural"
                              f" reference only, not as final output */\n{code}")
    if not parts:
        return ""
    return "=== Ghidra reference (structure hint, may not compile) ===\n" + \
           "\n\n".join(parts)


# ─── ppc2cpp semantic equivalence ────────────────────────────────────────────

def _ppc2cpp_available() -> bool:
    return PPC2CPP_CLI.exists() and os.access(PPC2CPP_CLI, os.X_OK)


def _ppc2cpp_target_project() -> Path | None:
    """
    Build (once per session) a ppc2cpp project from the target DOL.
    Returns path to the .ppc2cpp project file, or None if unavailable.
    """
    global _PPC2CPP_TARGET_PROJECT
    if _PPC2CPP_TARGET_PROJECT is not None:
        return _PPC2CPP_TARGET_PROJECT

    if not _ppc2cpp_available():
        return None

    dol = PROJECT_ROOT / "build" / "RUUE01" / "main.dol"
    if not dol.exists():
        return None

    proj = PROJECT_ROOT / "build" / "RUUE01" / "target.ppc2cpp"
    if not proj.exists():
        print("  🔬  Building ppc2cpp target project from DOL (once) …", flush=True)
        r = subprocess.run(
            [str(PPC2CPP_CLI), "create", "-o", str(proj), str(dol)],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"  ⚠  ppc2cpp create failed: {r.stderr[:200]}")
            return None
        print("  ✓  ppc2cpp target project ready")

    _PPC2CPP_TARGET_PROJECT = proj
    return proj


def ppc2cpp_checkflow(unit_name: str, fn_names: list[str]) -> bool:
    """
    Use ppc2cpp checkflow to test dataflow-graph equivalence between our
    compiled .o and the target DOL for the given function names.
    Returns True if ALL listed functions are equivalent (exit code 0).
    Silently returns False if ppc2cpp is not installed or projects can't be built.
    """
    target_proj = _ppc2cpp_target_project()
    if not target_proj:
        return False

    obj_path = PROJECT_ROOT / "build" / "RUUE01" / "obj" / f"{unit_name}.o"
    if not obj_path.exists():
        return False

    ours_proj = PROJECT_ROOT / "build" / "RUUE01" / "ours.ppc2cpp"
    r = subprocess.run(
        [str(PPC2CPP_CLI), "create", "-o", str(ours_proj), str(obj_path)],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False

    # checkflow accepts multiple function names positionally after the two projects
    cmd = [str(PPC2CPP_CLI), "checkflow", str(target_proj), str(ours_proj)] + fn_names
    r = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    return r.returncode == 0


def get_unit_entry(report: dict, unit_name: str) -> dict:
    full = f"main/{unit_name}"
    for u in report.get("units", []):
        if u["name"] == full:
            return u
    return {}


def get_match_pct(report: dict, unit_name: str) -> float:
    return get_unit_entry(report, unit_name).get("measures", {}).get("fuzzy_match_percent", 0.0)


def format_function_breakdown(report: dict, unit_name: str) -> str:
    fns = get_unit_entry(report, unit_name).get("functions", [])
    if not fns:
        return ""
    lines = []
    for fn in fns:
        pct  = fn.get("fuzzy_match_percent")
        name = fn.get("name", "?")
        size = fn.get("size", "?")
        if pct is None:
            lines.append(f"    {name} ({size}B): no match")
        else:
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            lines.append(f"    {name} ({size}B): {pct:5.1f}%  [{bar}]")
    return "\n".join(lines)


def asm_stats(asm_text: str) -> tuple[int, int]:
    """Return (byte_size, function_count) estimated from the .s file."""
    # Size: look for 'size: 0xXX' annotations
    sizes = re.findall(r"size:\s*0x([0-9A-Fa-f]+)", asm_text)
    total = sum(int(s, 16) for s in sizes) if sizes else len(asm_text) // 10
    funcs = len(re.findall(r"^\.fn\s", asm_text, re.MULTILINE))
    return total, max(funcs, 1)


# ─── Per-function mode (for large units with many functions) ──────────────────

# Units with more functions than this switch to per-function parallel mode.
# Set low so most units benefit from the parallel decomposition strategy.
# Whole-unit mode is only used for truly tiny units (≤ this many functions).
LARGE_UNIT_THRESHOLD = 8 if _PROFILE == "windows" else 5

# Skip threshold — set astronomically high so NO unit is ever skipped.
# Previously 35 (windows) / 20 (mac) — this was silently discarding 600+ units.
# Per-function parallel mode handles arbitrarily large units gracefully.
SKIP_UNIT_THRESHOLD = 9999


def parse_asm_functions(asm_text: str) -> list[dict]:
    """
    Split a dtk assembly file into individual .fn/.endfn blocks.
    Returns list of {"name": str, "asm": str, "size": int}.
    """
    blocks = []
    pattern = re.compile(
        r'^(\.fn\s+(\w+)[^\n]*\n.*?^\.endfn[^\n]*)',
        re.MULTILINE | re.DOTALL,
    )
    for m in pattern.finditer(asm_text):
        fn_asm  = m.group(1)
        fn_name = m.group(2)
        size_m  = re.search(r"size:\s*0x([0-9A-Fa-f]+)", fn_asm)
        fn_size = int(size_m.group(1), 16) if size_m else 0
        blocks.append({"name": fn_name, "asm": fn_asm, "size": fn_size})
    return blocks


def _strip_fn_output(c_code: str) -> str:
    """
    Strip markdown fences, #include lines, and // comments from a
    single-function LLM response.  We only want the function body itself.
    """
    c_code = re.sub(r"^```[a-z+]*\s*\n?", "", c_code, flags=re.MULTILINE)
    c_code = re.sub(r"^```\s*$",           "", c_code, flags=re.MULTILINE)
    c_code = re.sub(r'(?<!:)//[^\n]*',     "", c_code)
    # Drop #include / #pragma / extern lines — header handles those
    c_code = re.sub(r'^\s*#(include|pragma)\s+.*$', "", c_code, flags=re.MULTILINE)
    return c_code.strip()


def decompile_single_fn(fn_name: str, fn_asm: str, ctx: str,
                         start_level: int, max_attempts: int,
                         url_override: str | None = None,
                         strategy: str = "") -> str:
    """
    Ask the LLM to decompile one function.
    Returns the C function code (no headers), or '' on total failure.

    url_override: target a specific Ollama port (parallel worker routing).
    strategy:     pre-analysis notes from council phase to inject into prompt.
    """
    prev_c        = ""
    compile_error = ""

    for attempt in range(max_attempts):
        level = min(start_level + attempt, len(MODELS) - 1)

        parts = []
        if strategy and attempt == 0:
            parts.append(strategy)
        if ctx:
            parts.append(ctx)
        parts.append(
            f"=== Target assembly for function {fn_name} ===\n{fn_asm}\n\n"
            "Decompile the assembly above into a single C/C++ function.\n"
            "Output ONLY the function definition — no #include lines, no extern "
            "declarations, no file-level boilerplate. Just the function itself."
        )
        if compile_error:
            parts.append(
                f"Previous attempt failed to compile:\n{prev_c}\n\n"
                f"Compiler error:\n{compile_error}\n\nFix the error."
            )
        elif prev_c:
            parts.append(
                f"Previous attempt (didn't match 100%):\n{prev_c}\n\n"
                "Rewrite to fix mismatches. Keep function signature identical."
            )

        prompt = "\n\n".join(parts)
        try:
            c_code, _ = generate_c(level, prompt, url_override=url_override)
        except RuntimeError:
            break

        c_code = _strip_fn_output(c_code)
        if c_code:
            prev_c = c_code

    return prev_c


# ─── Multi-model council: pre-analysis strategy ───────────────────────────────

def _council_strategy(unit_name: str, asm: str, ctx: str) -> str:
    """
    Run a fast 7B analysis pass to identify calling conventions, struct offsets,
    signedness patterns, and tail calls.  The result is injected into every
    full-decomp prompt so stronger models don't start cold.

    Only runs on Windows (dual-GPU) for units with 3+ functions — the overhead
    is not worth it for tiny units.  Uses the GTX 1060 (port 11435) so the
    RTX 5060 Ti remains free for the primary decomp attempt.
    """
    _, func_count = asm_stats(asm)
    if _PROFILE != "windows" or func_count < 3:
        return ""

    # Use the 7B screener on the 1060 — fast, doesn't block the 5060 Ti
    screener = next((m for m in MODELS if m["id"] == "qwen7b"), None)
    if screener is None:
        return ""

    # First 120 lines of assembly is enough for structural analysis
    asm_head = "\n".join(asm.splitlines()[:120])
    ctx_head = ctx[:1500] if ctx else ""

    strategy_prompt = (
        f"Analyze this PowerPC assembly for unit '{unit_name}' and give a brief "
        "strategy note — NOT code, just observations.\n\n"
        + (f"{ctx_head}\n\n" if ctx_head else "")
        + f"=== Assembly (excerpt) ===\n{asm_head}\n\n"
        "Answer ONLY the following in short bullet points (150 words max):\n"
        "1. Parameters: which r3-r10 registers are read before being written at entry?\n"
        "2. Return type: what is in r3 just before blr?\n"
        "3. Struct offsets: any notable lwz/stw offsets that reveal field layout?\n"
        "4. Tail calls: any bare `b fn_XXXX` (no bl) = tail call, note the callee.\n"
        "5. Signedness: cmplw/srwi = unsigned, cmpw/srawi = signed — which dominates?\n"
        "6. Any pattern that would trip up a decompiler (loop, switch, inline fn)?\n"
        "Output ONLY the bullet list. No code, no markdown fences."
    )

    try:
        analysis = call_ollama(
            screener["name"], strategy_prompt, timeout=90,
            url=screener.get("url"),
            system="You are a PowerPC assembly analyst. Be terse and precise.",
        )
        # Strip any code blocks the model sneaked in
        analysis = re.sub(r"```.*?```", "", analysis, flags=re.DOTALL).strip()
        if len(analysis) > 60:
            return (
                "=== Pre-analysis (7B strategy council — use as structural guide) ===\n"
                + analysis
            )
    except Exception as _e:
        pass   # silently skip — council failure must never block decompilation
    return ""


def _parallel_decompile_functions(
    functions: list[dict],
    ctx: str,
    start_level: int,
    max_attempts: int,
    strategy: str,
) -> dict[str, str]:
    """
    Decompile a list of functions in parallel using both GPU workers.

    On Windows dual-GPU:
      Even-indexed functions → port 11435 (GTX 1060, 7B)
      Odd-indexed functions  → port 11434 (RTX 5060 Ti, 14B)
    Both workers run concurrently so both GPUs stay busy.

    For heavy models (tier ≥ 2, span both GPUs), falls back to sequential
    processing to avoid VRAM contention.

    Returns dict of fn_name → c_code_string.
    """
    # Heavy models need both GPUs — can't parallelise at tier 2+
    use_parallel = _PROFILE == "windows" and start_level < 2 and len(_WORKER_URLS) >= 2

    fn_bodies: dict[str, str] = {}

    if not use_parallel or len(functions) < 2:
        # Sequential fallback
        for i, fn in enumerate(functions, 1):
            fn_name = fn["name"]
            print(f"\n  [{i:>3}/{len(functions)}]  {fn_name}  ({fn['size']}B)", flush=True)
            body = decompile_single_fn(fn_name, fn["asm"], ctx, start_level,
                                       max_attempts, strategy=strategy)
            fn_bodies[fn_name] = body if body else f"/* TODO: {fn_name} */"
        return fn_bodies

    # Parallel mode — assign functions round-robin to the two GPU ports
    def _worker(idx: int, fn: dict) -> tuple[str, str]:
        url = _WORKER_URLS[idx % len(_WORKER_URLS)]
        # Match the worker's port to the right model level
        worker_level = 0 if "11435" in url else 1   # 7B on 1060, 14B on 5060 Ti
        effective_level = max(start_level, worker_level)
        body = decompile_single_fn(
            fn["name"], fn["asm"], ctx,
            effective_level, max_attempts,
            url_override=url,
            strategy=strategy,
        )
        return fn["name"], body if body else f"/* TODO: {fn['name']} */"

    total = len(functions)
    completed = 0
    print(f"  ⚡  Parallel mode: {total} functions across {len(_WORKER_URLS)} GPU workers",
          flush=True)

    with ThreadPoolExecutor(max_workers=len(_WORKER_URLS)) as ex:
        futures = {ex.submit(_worker, i, fn): fn for i, fn in enumerate(functions)}
        for fut in as_completed(futures):
            fn_name, body = fut.result()
            fn_bodies[fn_name] = body
            completed += 1
            if completed % 5 == 0 or completed == total:
                print(f"  ⚡  {completed}/{total} functions done", flush=True)

    # Restore insertion order (dict preserves insertion, but futures complete randomly)
    ordered: dict[str, str] = {}
    for fn in functions:
        ordered[fn["name"]] = fn_bodies.get(fn["name"], f"/* TODO: {fn['name']} */")
    return ordered


def process_large_unit(
    unit_name: str,
    asm: str,
    ctx: str,
    max_attempts: int,
    start_level: int,
    no_commit: bool,
) -> bool:
    """
    Decompile a unit function-by-function, then build the combined file.
    Used automatically when func_count > LARGE_UNIT_THRESHOLD.
    Returns True if the unit reaches 100% match.
    """
    functions = parse_asm_functions(asm)
    if not functions:
        print("  ⚠  Could not parse individual functions — falling back to whole-unit mode")
        return False  # caller will fall through to normal path

    asm_size, func_count = asm_stats(asm)
    print(f"  🔀  Per-function mode: {len(functions)} functions to decompile")

    # ── Council: pre-analysis strategy ────────────────────────────────────────
    strategy = ""
    if len(functions) >= 3:
        print("  🗣  Council analysis (7B strategy pass) …", end="", flush=True)
        t_c = time.time()
        strategy = _council_strategy(unit_name, asm, ctx)
        print(f" done ({time.time()-t_c:.1f}s)")
        if strategy:
            print(f"  💡  Strategy injected into prompts")

    # ── Pass 1: generate each function (parallel on dual-GPU) ─────────────────
    fn_bodies = _parallel_decompile_functions(
        functions, ctx, start_level, max_attempts, strategy,
    )

    # ── Assemble combined source ───────────────────────────────────────────────
    header = "#include <dolphin/types.h>\n"
    combined = header + "\n\n".join(fn_bodies.values()) + "\n"
    write_source(unit_name, combined)

    # ── Build ──────────────────────────────────────────────────────────────────
    ok, build_err = build_unit(unit_name)

    # ── Pass 2: fix compile errors function-by-function ───────────────────────
    if not ok:
        print(f"\n  🔨  Combined file has errors — fixing individually …")
        # identify which function is broken by the line number in the error
        line_m = re.search(r"Line (\d+):", build_err)
        if line_m:
            err_line = int(line_m.group(1))
            # Map line number → function name
            src_lines = combined.splitlines()
            # Scan upward from err_line for a function def
            broken_fn = None
            for ln in range(min(err_line - 1, len(src_lines) - 1), -1, -1):
                nm = re.match(r"(?:[\w*\s]+\s+)?(\w+)\s*\(", src_lines[ln])
                if nm:
                    candidate = nm.group(1)
                    if candidate in fn_bodies:
                        broken_fn = candidate
                        break
            if broken_fn:
                print(f"  🔧  Retrying {broken_fn} …")
                fn_data = next((f for f in functions if f["name"] == broken_fn), None)
                if fn_data:
                    fixed = decompile_single_fn(
                        broken_fn, fn_data["asm"], ctx,
                        start_level, max_attempts,
                    )
                    if fixed:
                        fn_bodies[broken_fn] = fixed
                        combined = header + "\n\n".join(fn_bodies.values()) + "\n"
                        write_source(unit_name, combined)
                        ok, build_err = build_unit(unit_name)

    if not ok:
        print(f"  ✗  Still failing to compile after per-fn fixes: {build_err[:200]}")
        record_result(unit_name, MODELS[start_level]["id"], start_level,
                      False, 0.0, asm_size, func_count)
        return False

    # ── Check match % ──────────────────────────────────────────────────────────
    try:
        report = regenerate_report()
        match  = get_match_pct(report, unit_name)
    except Exception as e:
        print(f"  ⚠  Match check failed: {e}")
        record_result(unit_name, MODELS[start_level]["id"], start_level,
                      False, 0.0, asm_size, func_count)
        return False

    bd = format_function_breakdown(report, unit_name)
    print(f"\n  📊  Overall: {match:.1f}%")
    if bd:
        print(bd)

    # ── Pass 3: retry worst functions with a stronger model ────────────────────
    fn_entries = get_unit_entry(report, unit_name).get("functions", [])
    broken_fns = [f for f in fn_entries if (f.get("fuzzy_match_percent") or 0) < 80.0]

    if broken_fns and start_level < len(MODELS) - 1:
        stronger = min(start_level + 1, len(MODELS) - 1)
        print(f"\n  🔁  {len(broken_fns)} function(s) below 80% — retrying with stronger model (level {stronger}) …")
        retry_fns = []
        for fn_info in broken_fns:
            fn_name = fn_info["name"]
            fn_data = next((f for f in functions if f["name"] == fn_name), None)
            if fn_data:
                print(f"     ↑  {fn_name}  ({fn_info.get('fuzzy_match_percent', 0):.1f}%)")
                retry_fns.append(fn_data)
        if retry_fns:
            retry_bodies = _parallel_decompile_functions(
                retry_fns, ctx, stronger, max_attempts, strategy,
            )
            for fn_name, body in retry_bodies.items():
                if body and not body.startswith("/* TODO:"):
                    fn_bodies[fn_name] = body

        combined = header + "\n\n".join(fn_bodies.values()) + "\n"
        write_source(unit_name, combined)
        ok2, _ = build_unit(unit_name)
        if ok2:
            try:
                report = regenerate_report()
                match  = get_match_pct(report, unit_name)
                bd     = format_function_breakdown(report, unit_name)
                print(f"\n  📊  After retry: {match:.1f}%")
                if bd:
                    print(bd)
            except Exception:
                pass

    if match >= 100.0:
        print(f"\n  🎉  100% match — {unit_name}")
        record_result(unit_name, MODELS[start_level]["id"], start_level,
                      True, match, asm_size, func_count)
        if not no_commit:
            git_commit_and_push(unit_name, match)
        return True

    record_result(unit_name, MODELS[start_level]["id"], start_level,
                  False, match, asm_size, func_count)
    print(f"  ✓  {unit_name}: {match:.1f}% — saved (not 100% but better than nothing)")
    if not no_commit and match > 0:
        git_commit_and_push(unit_name, match)
    return False


# ─── Git helpers ──────────────────────────────────────────────────────────────

GIT_AUTHOR = "strayreign"
GIT_EMAIL  = "strayreign@users.noreply.github.com"


def git_commit_and_push(unit_name: str, match_pct: float) -> bool:
    """
    Squash the entire repo history to a single commit authored by strayreign,
    then force-push to origin/main.  This keeps the remote clean — one commit
    per successful match rather than an ever-growing history.
    """
    # Build a commit message showing what changed
    pct_str = f"{match_pct:.1f}%"
    status  = "✓" if match_pct >= 100.0 else f"~{pct_str}"
    msg = f"[autopilot] {status} {unit_name}"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME":    GIT_AUTHOR,
        "GIT_AUTHOR_EMAIL":   GIT_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR,
        "GIT_COMMITTER_EMAIL": GIT_EMAIL,
    }

    print("  📝  Squashing history → single commit …")

    def run(args, **kw):
        r = subprocess.run(args, cwd=PROJECT_ROOT, env=env,
                           capture_output=True, text=True, **kw)
        if r.returncode != 0:
            raise RuntimeError(
                f"git {args[1]} failed:\n{r.stderr.strip()[:400]}"
            )
        return r

    try:
        # 0. Regenerate objdiff.json from objects.json so decomp.dev categories are fresh
        _cfg = subprocess.run(
            [sys.executable, "configure.py"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        if _cfg.returncode != 0:
            print(f"  ⚠  configure.py failed (non-fatal):\n{_cfg.stderr.strip()[:200]}")

        # 1. Create an orphan branch (no history)
        run(["git", "checkout", "--orphan", "_squash_tmp"])
        # 2. Stage everything
        run(["git", "add", "-A"])
        # 3. Single commit
        run(["git", "commit", "-m", msg])
        # 4. Delete main locally
        run(["git", "branch", "-D", "main"])
        # 5. Rename orphan to main
        run(["git", "branch", "-m", "main"])
        # 6. Force-push — record time just before so monitor ignores older runs
        push_time = time.time()
        token = os.environ.get("GITHUB_TOKEN", "")
        remote = (f"https://{token}@github.com/strayreign/accf-decomp.git"
                  if token else "origin")
        push = subprocess.run(
            ["git", "push", "--force", remote, "main"],
            cwd=PROJECT_ROOT, env=env, timeout=60,
            capture_output=True, text=True,
        )
        if push.returncode != 0:
            print(f"  ⚠  Push failed:\n{push.stderr.strip()[:400]}")
            return False

        # Only wait for CI on 100% byte matches — partial improvements just push
        # and continue so we don't burn 10 min per unit on CI waits
        if match_pct >= 100.0:
            print("  🚀  Pushed — waiting for CI …")
            monitor = PROJECT_ROOT / "tools" / "monitor.py"
            if monitor.exists():
                r = subprocess.run(
                    [sys.executable, str(monitor), "--pushed-at", str(push_time)],
                    cwd=PROJECT_ROOT,
                )
                if r.returncode != 0:
                    print("  ❌  CI failed — unit not counted as matched")
                    return False
        else:
            print(f"  🚀  Pushed ({match_pct:.1f}% — not waiting for CI on partial)")
        return True

    except RuntimeError as e:
        print(f"  ⚠  {e}")
        subprocess.run(["git", "checkout", "main"], cwd=PROJECT_ROOT,
                       capture_output=True)
        return False


# ─── Main loop ────────────────────────────────────────────────────────────────

def process_function(
    addr_or_unit: str,
    max_attempts: int = 5,
    start_model: str | None = None,
    dry_run: bool = False,
    no_commit: bool = False,
    verbose: bool = False,
) -> bool:
    print(f"\n{'='*64}")
    print(f"  🔧  {addr_or_unit}")

    try:
        unit_name = resolve_unit(addr_or_unit)
    except ValueError as e:
        print(f"  ✗  {e}")
        return False

    print(f"  Unit: {unit_name}")
    print(f"{'='*64}")

    try:
        asm = load_assembly(unit_name)
    except FileNotFoundError as e:
        print(f"  ✗  {e}")
        return False

    asm_size, func_count = asm_stats(asm)
    print(f"  ASM: ~{asm_size} bytes, {func_count} function(s)")

    ctx         = load_context(unit_name, asm)
    ghidra_ctx  = load_ghidra_context(unit_name, asm)
    if ghidra_ctx:
        print("  🧠  Ghidra context available")
    prev_c = load_current_source(unit_name)

    # Baseline match
    try:
        report     = regenerate_report()
        prev_match = get_match_pct(report, unit_name)
    except Exception as e:
        print(f"  \u26a0  Could not read report: {e}")
        prev_match = 0.0

    if prev_match >= 100.0:
        print("  \u2705  Already 100% \u2014 nothing to do.")
        return True

    print(f"  Baseline: {prev_match:.1f}%")
    bd = format_function_breakdown(report if 'report' in dir() else {}, unit_name)
    if bd:
        print(bd)

    if dry_run:
        print("\n=== DRY RUN \u2014 sample prompt ===")
        print(build_prompt(unit_name, asm, ctx, prev_c, prev_match, 0,
                           ghidra_ctx=ghidra_ctx)[:3000])
        return False

    # Determine starting model level
    if start_model:
        if start_model not in MODEL_IDS:
            print(f"  \u2717  Unknown model '{start_model}'. Choose from: {MODEL_IDS}")
            return False
        start_level = MODEL_IDS.index(start_model)
    else:
        start_level = smart_start_level(asm_size, func_count)
        if start_level > 0:
            print(f"  \U0001f4da  History says start at {MODELS[start_level]['label'].strip()}")

    # \u2500\u2500 Skip untractable mega-units \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    if func_count > SKIP_UNIT_THRESHOLD:
        print(f"  \u23ed  {func_count} functions \u2014 exceeds hard skip cap, skipping")
        return False

    # \u2500\u2500 Large-unit fast path: decompile function-by-function (parallel) \u2500\u2500\u2500
    if func_count > LARGE_UNIT_THRESHOLD:
        print(f"  \u26a1  {func_count} functions \u2014 switching to per-function parallel mode")
        return process_large_unit(
            unit_name, asm, ctx, max_attempts, start_level, no_commit,
        )

    # \u2500\u2500 Council strategy for whole-unit mode \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    _council_result: dict[str, str] = {}

    def _run_council():
        _council_result["strategy"] = _council_strategy(unit_name, asm, ctx)

    council_thread = threading.Thread(target=_run_council, daemon=True)
    council_thread.start()

    best_match    = prev_match
    best_c        = prev_c
    winning_level = None
    compile_error = ""
    escalations   = 0

    improvement_notes = _load_strategy_notes()

    for attempt in range(max_attempts):
        level = min(start_level + escalations, len(MODELS) - 1)
        print(f"\n  \u2500\u2500 Attempt {attempt + 1}/{max_attempts} (level {level}) \u2500\u2500")

        council_strategy_text = ""
        if attempt == 0:
            council_thread.join(timeout=120)
            council_strategy_text = _council_result.get("strategy", "")
            if council_strategy_text:
                print("  \U0001f4a1  Council strategy ready \u2014 injecting into prompt")

        diff_text = get_objdiff_diff(unit_name) if attempt > 0 and prev_c else ""
        fn_status = get_unit_entry(report, unit_name).get("functions", []) if attempt > 0 else None

        augmented_ctx = ctx
        extra_parts = []
        if council_strategy_text and attempt == 0:
            extra_parts.append(council_strategy_text)
        if improvement_notes and attempt == 0:
            extra_parts.append(
                "=== Lessons from previous decompilation runs ===\n" + improvement_notes
            )
        if extra_parts:
            augmented_ctx = "\n\n".join(extra_parts + ([ctx] if ctx else []))

        prompt = build_prompt(unit_name, asm, augmented_ctx, prev_c, prev_match, attempt,
                              diff_text, fn_status, compile_error,
                              ghidra_ctx=ghidra_ctx if attempt == 0 else "")
        compile_error = ""

        if verbose:
            print(prompt[:1500])

        try:
            c_code, used_level = generate_c(level, prompt)
        except RuntimeError as e:
            print(f"  \u2717  {e}")
            break

        c_code = _extract_c_code(c_code)
        if re.search(r'(?<![:\w])//(?!/)', c_code):
            c_code = re.sub(r'(?<![:\w])//([^\n]*)', r'/* \1 */', c_code)
            print("  \U0001f527  Auto-fixed // comments \u2192 /* */")

        val_err = _prevalidate(c_code)
        if val_err:
            print(f"  \u26a0  Pre-validation: {val_err}")
            compile_error = f"Code validation failed before compile:\n{val_err}"
            prev_c        = c_code
            escalations  += 1
            continue

        write_source(unit_name, c_code)

        ok, build_err = build_unit(unit_name)
        if not ok:
            prev_c        = c_code
            prev_match    = 0.0
            compile_error = build_err
            escalations  += 1
            continue

        try:
            report = regenerate_report()
            match  = get_match_pct(report, unit_name)
        except Exception as e:
            print(f"  \u26a0  Match check failed: {e}")
            prev_c = c_code
            continue

        bd = format_function_breakdown(report, unit_name)
        print(f"  \U0001f4ca  {match:.1f}%")
        if bd:
            print(bd)

        if match > best_match:
            best_match    = match
            best_c        = c_code
            winning_level = used_level

        if match >= 100.0:
            print(f"\n  \U0001f389  100% match \u2014 {unit_name}")
            record_result(unit_name, MODELS[used_level]["id"], used_level,
                          True, match, asm_size, func_count)
            if used_level > 0:
                note = (
                    f"Unit size bucket '{_size_bucket(asm_size)}' "
                    f"({func_count} fn, {asm_size}B) solved by "
                    f"{MODELS[used_level]['id']} on attempt {attempt+1}"
                )
                append_strategy_note(note)
            if not no_commit:
                git_commit_and_push(unit_name, match)
            return True

        if match > 0 and _ppc2cpp_available():
            fn_syms = [fn.get("name", "") for fn in
                       get_unit_entry(report, unit_name).get("functions", [])
                       if fn.get("name")]
            if fn_syms and ppc2cpp_checkflow(unit_name, fn_syms):
                print(f"\n  \U0001f52c  ppc2cpp: semantically equivalent ({match:.1f}%) \u2014 {unit_name}")
                record_result(unit_name, MODELS[used_level]["id"], used_level,
                              True, match, asm_size, func_count)
                if not no_commit:
                    git_commit_and_push(unit_name, match)
                return True

        prev_c     = c_code
        prev_match = match

    if best_c and best_match > 0:
        print(f"\n  \u21a9  Best was {best_match:.1f}% \u2014 restoring ...")
        write_source(unit_name, best_c)

    final_level = winning_level if winning_level is not None else min(start_level + escalations, len(MODELS) - 1)
    record_result(unit_name, MODELS[final_level]["id"], final_level,
                  False, best_match, asm_size, func_count)

    print(f"  \u2717  {unit_name}: best {best_match:.1f}% after {max_attempts} attempts")
    if not no_commit and best_match > 0:
        git_commit_and_push(unit_name, best_match)
    return False


# \u2500\u2500\u2500 Git helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

GIT_AUTHOR = "strayreign"
GIT_EMAIL  = "strayreign@users.noreply.github.com"


def git_commit_and_push(unit_name: str, match_pct: float) -> bool:
    """
    Squash the entire repo history to a single commit authored by strayreign,
    then force-push to origin/main.
    """
    pct_str = f"{match_pct:.1f}%"
    status  = "\u2713" if match_pct >= 100.0 else f"~{pct_str}"
    msg = f"[autopilot] {status} {unit_name}"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME":     GIT_AUTHOR,
        "GIT_AUTHOR_EMAIL":    GIT_EMAIL,
        "GIT_COMMITTER_NAME":  GIT_AUTHOR,
        "GIT_COMMITTER_EMAIL": GIT_EMAIL,
    }

    print("  \U0001f4dd  Squashing history \u2192 single commit ...")

    def _run(args, **kw):
        r = subprocess.run(args, cwd=PROJECT_ROOT, env=env,
                           capture_output=True, text=True, **kw)
        if r.returncode != 0:
            raise RuntimeError(f"git {args[1]} failed:\n{r.stderr.strip()[:400]}")
        return r

    try:
        _run(["git", "checkout", "--orphan", "_squash_tmp"])
        _run(["git", "add", "-A"])
        _run(["git", "commit", "-m", msg])
        _run(["git", "branch", "-D", "main"])
        _run(["git", "branch", "-m", "main"])
        push_time = time.time()
        token  = os.environ.get("GITHUB_TOKEN", "")
        remote = (f"https://{token}@github.com/strayreign/accf-decomp.git"
                  if token else "origin")
        push = subprocess.run(
            ["git", "push", "--force", remote, "main"],
            cwd=PROJECT_ROOT, env=env, timeout=60,
            capture_output=True, text=True,
        )
        if push.returncode != 0:
            print(f"  \u26a0  Push failed:\n{push.stderr.strip()[:400]}")
            return False

        if match_pct >= 100.0:
            print("  \U0001f680  Pushed \u2014 waiting for CI ...")
            monitor = PROJECT_ROOT / "tools" / "monitor.py"
            if monitor.exists():
                r = subprocess.run(
                    [sys.executable, str(monitor), "--pushed-at", str(push_time)],
                    cwd=PROJECT_ROOT,
                )
                if r.returncode != 0:
                    print("  \u274c  CI failed \u2014 unit not counted as matched")
                    return False
        else:
            print(f"  \U0001f680  Pushed ({match_pct:.1f}% \u2014 not waiting for CI on partial)")
        return True

    except RuntimeError as e:
        print(f"  \u26a0  {e}")
        subprocess.run(["git", "checkout", "main"], cwd=PROJECT_ROOT, capture_output=True)
        return False


# \u2500\u2500\u2500 Entry point \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def main():
    parser = argparse.ArgumentParser(
        description="Automated ACCF decompilation loop \u2014 fires LLMs until 100% match."
    )
    parser.add_argument("unit", help="Address (802C5394) or unit name (auto_03_802C5394_text)")
    parser.add_argument("--max-attempts", type=int, default=5,
                        help="Max LLM attempts (default 5)")
    parser.add_argument("--start-model", choices=MODEL_IDS, default=None,
                        help="Force a specific starting model (overrides history)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print prompt and exit without calling any LLM")
    parser.add_argument("--no-commit", action="store_true",
                        help="Skip git commit/push even on 100%%")
    parser.add_argument("--verbose", action="store_true",
                        help="Print first 1500 chars of each prompt")
    args = parser.parse_args()

    ok = process_function(
        args.unit,
        max_attempts = args.max_attempts,
        start_model  = args.start_model,
        dry_run      = args.dry_run,
        no_commit    = args.no_commit,
        verbose      = args.verbose,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
