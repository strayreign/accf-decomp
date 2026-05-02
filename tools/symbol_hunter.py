#!/usr/bin/env python3
"""
symbol_hunter.py — Automated symbol naming for ACCF (RUUE01).

ACCF has no public debug symbols. This script builds up a picture of what
functions do by combining several sources:

  1. String references  — if a function loads a visible ASCII string,
                          that string becomes a naming hint.
  2. Known SDK patterns — Dolphin OS/GX/DVD functions have well-known
                          signatures; we match by size + call pattern.
  3. Cross-references   — if a 100%-matched function calls fn_XXXXXXXX,
                          we can propagate context to the callee.
  4. Matched-function   — names already present in source files are
     scanning            extracted and used to name their callers/callees.

Outputs:
  • data/symbol_hints.json  — hints dict  {fn_XXXXXXXX: {name, source, confidence}}
  • tools/symbol_report.txt — human-readable summary

Usage:
  python3 tools/symbol_hunter.py
  python3 tools/symbol_hunter.py --apply   # write inferred names to symbols.txt
  python3 tools/symbol_hunter.py --report  # print summary and exit
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASM_DIR      = PROJECT_ROOT / "build" / "RUUE01" / "asm"
SRC_DIR      = PROJECT_ROOT / "src"
SYMBOLS_FILE = PROJECT_ROOT / "config" / "RUUE01" / "symbols.txt"
HINTS_FILE   = PROJECT_ROOT / "data" / "symbol_hints.json"
REPORT_FILE  = PROJECT_ROOT / "tools" / "symbol_report.txt"
OBJDIFF_JSON = PROJECT_ROOT / "objdiff.json"


# ─── Known Dolphin SDK signatures ─────────────────────────────────────────────
# (size_bytes, call_pattern_fragments) → probable name
# Size is approximate (±8 bytes), call_pattern is substrings to look for in ASM

SDK_SIGNATURES: list[dict] = [
    # ── Dolphin OS ──────────────────────────────────────────────────────────
    {"size_range": (12, 24),  "calls": [],                     "name": "OSGetArenaHi",         "lib": "dolphin/os"},
    {"size_range": (12, 24),  "calls": [],                     "name": "OSGetArenaLo",         "lib": "dolphin/os"},
    {"size_range": (12, 24),  "calls": [],                     "name": "OSSetArenaHi",         "lib": "dolphin/os"},
    {"size_range": (12, 24),  "calls": [],                     "name": "OSSetArenaLo",         "lib": "dolphin/os"},
    {"size_range": (40, 80),  "calls": ["OSCreateThread"],     "name": "OSInitThread",         "lib": "dolphin/os"},
    {"size_range": (20, 48),  "calls": [],                     "name": "OSDisableInterrupts",  "lib": "dolphin/os"},
    {"size_range": (20, 48),  "calls": [],                     "name": "OSEnableInterrupts",   "lib": "dolphin/os"},
    {"size_range": (20, 48),  "calls": [],                     "name": "OSRestoreInterrupts",  "lib": "dolphin/os"},
    {"size_range": (24, 64),  "calls": ["OSDisableInterrupts"],"name": "OSSetCurrentContext",  "lib": "dolphin/os"},
    {"size_range": (8,  20),  "calls": [],                     "name": "OSGetCurrentContext",  "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": ["OSInitMutex"],        "name": "OSInitMutex",          "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": ["OSLockMutex"],        "name": "OSLockMutex",          "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": ["OSUnlockMutex"],      "name": "OSUnlockMutex",        "lib": "dolphin/os"},
    {"size_range": (40, 80),  "calls": ["OSSleepThread"],      "name": "OSSleepThread",        "lib": "dolphin/os"},
    {"size_range": (20, 48),  "calls": ["OSWakeupThread"],     "name": "OSWakeupThread",       "lib": "dolphin/os"},
    {"size_range": (120,200), "calls": [],                     "name": "OSReport",             "lib": "dolphin/os"},
    {"size_range": (40, 80),  "calls": ["OSReport"],           "name": "OSPanic",              "lib": "dolphin/os"},
    {"size_range": (300,600), "calls": [],                     "name": "__OSInitSystemCall",   "lib": "dolphin/os"},
    {"size_range": (20, 60),  "calls": [],                     "name": "OSGetTick",            "lib": "dolphin/os"},
    {"size_range": (20, 60),  "calls": [],                     "name": "OSGetTime",            "lib": "dolphin/os"},
    {"size_range": (20, 60),  "calls": [],                     "name": "OSTick2Milliseconds",  "lib": "dolphin/os"},
    {"size_range": (60, 120), "calls": [],                     "name": "OSInitAlarm",          "lib": "dolphin/os"},
    {"size_range": (60, 160), "calls": ["OSSetAlarm"],         "name": "OSSetAlarm",           "lib": "dolphin/os"},
    {"size_range": (20, 60),  "calls": [],                     "name": "OSCancelAlarm",        "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": [],                     "name": "DCFlushRange",         "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": [],                     "name": "DCInvalidateRange",    "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": [],                     "name": "DCStoreRange",         "lib": "dolphin/os"},
    {"size_range": (40, 100), "calls": [],                     "name": "ICInvalidateRange",    "lib": "dolphin/os"},
    {"size_range": (20, 60),  "calls": [],                     "name": "PPCMtmsr",             "lib": "dolphin/os"},
    {"size_range": (20, 60),  "calls": [],                     "name": "PPCMfmsr",             "lib": "dolphin/os"},
    # ── Dolphin OS Heap ─────────────────────────────────────────────────────
    {"size_range": (80, 200), "calls": [],                     "name": "OSInitHeap",           "lib": "dolphin/os"},
    {"size_range": (60, 200), "calls": [],                     "name": "OSAllocFromHeap",      "lib": "dolphin/os"},
    {"size_range": (40, 120), "calls": [],                     "name": "OSFreeToHeap",         "lib": "dolphin/os"},
    # ── Dolphin DVD ─────────────────────────────────────────────────────────
    {"size_range": (200,500), "calls": [],                     "name": "DVDOpen",              "lib": "dolphin/dvd"},
    {"size_range": (60, 150), "calls": [],                     "name": "DVDClose",             "lib": "dolphin/dvd"},
    {"size_range": (200,500), "calls": [],                     "name": "DVDRead",              "lib": "dolphin/dvd"},
    {"size_range": (60, 150), "calls": [],                     "name": "DVDGetFileInfo",       "lib": "dolphin/dvd"},
    {"size_range": (60, 150), "calls": ["DVDOpen"],            "name": "DVDConvertPathToEntrynum", "lib": "dolphin/dvd"},
    # ── Dolphin GX ──────────────────────────────────────────────────────────
    {"size_range": (300,600), "calls": [],                     "name": "GXInit",               "lib": "dolphin/gx"},
    {"size_range": (40, 100), "calls": [],                     "name": "GXBegin",              "lib": "dolphin/gx"},
    {"size_range": (20, 60),  "calls": [],                     "name": "GXEnd",                "lib": "dolphin/gx"},
    {"size_range": (40, 100), "calls": [],                     "name": "GXSetVtxDesc",         "lib": "dolphin/gx"},
    {"size_range": (40, 100), "calls": [],                     "name": "GXSetVtxAttrFmt",      "lib": "dolphin/gx"},
    {"size_range": (60, 150), "calls": [],                     "name": "GXLoadPosMtxImm",      "lib": "dolphin/gx"},
    {"size_range": (60, 150), "calls": [],                     "name": "GXLoadNrmMtxImm",      "lib": "dolphin/gx"},
    {"size_range": (40, 100), "calls": [],                     "name": "GXSetProjection",      "lib": "dolphin/gx"},
    {"size_range": (20, 60),  "calls": [],                     "name": "GXSetScissor",         "lib": "dolphin/gx"},
    {"size_range": (20, 60),  "calls": [],                     "name": "GXSetViewport",        "lib": "dolphin/gx"},
    {"size_range": (40, 100), "calls": [],                     "name": "GXSetCullMode",        "lib": "dolphin/gx"},
    {"size_range": (20, 60),  "calls": [],                     "name": "GXClearVtxDesc",       "lib": "dolphin/gx"},
    # ── Dolphin MTX ─────────────────────────────────────────────────────────
    {"size_range": (60, 150), "calls": [],                     "name": "MTXIdentity",          "lib": "dolphin/mtx"},
    {"size_range": (80, 200), "calls": [],                     "name": "MTXMultiply",          "lib": "dolphin/mtx"},
    {"size_range": (60, 150), "calls": [],                     "name": "MTXTransApply",        "lib": "dolphin/mtx"},
    {"size_range": (60, 150), "calls": [],                     "name": "MTXScale",             "lib": "dolphin/mtx"},
    {"size_range": (60, 150), "calls": [],                     "name": "MTXRotRad",            "lib": "dolphin/mtx"},
    {"size_range": (60, 150), "calls": [],                     "name": "MTXLookAt",            "lib": "dolphin/mtx"},
    {"size_range": (60, 150), "calls": [],                     "name": "MTXFrustum",           "lib": "dolphin/mtx"},
    {"size_range": (60, 150), "calls": [],                     "name": "MTXOrtho",             "lib": "dolphin/mtx"},
    # ── Dolphin VI ──────────────────────────────────────────────────────────
    {"size_range": (200,500), "calls": [],                     "name": "VIInit",               "lib": "dolphin/vi"},
    {"size_range": (40, 120), "calls": ["VIInit"],             "name": "VIConfigure",          "lib": "dolphin/vi"},
    {"size_range": (20, 60),  "calls": [],                     "name": "VIFlush",              "lib": "dolphin/vi"},
    {"size_range": (20, 60),  "calls": [],                     "name": "VISetNextFrameBuffer", "lib": "dolphin/vi"},
    {"size_range": (20, 60),  "calls": [],                     "name": "VIGetNextField",       "lib": "dolphin/vi"},
    {"size_range": (20, 60),  "calls": [],                     "name": "VIWaitForRetrace",     "lib": "dolphin/vi"},
    # ── Dolphin PAD ─────────────────────────────────────────────────────────
    {"size_range": (100,300), "calls": [],                     "name": "PADInit",              "lib": "dolphin/pad"},
    {"size_range": (100,400), "calls": [],                     "name": "PADRead",              "lib": "dolphin/pad"},
    # ── RVL (Wii) ────────────────────────────────────────────────────────────
    {"size_range": (100,300), "calls": [],                     "name": "WPADInit",             "lib": "rvl/wpad"},
    {"size_range": (100,300), "calls": [],                     "name": "WPADRead",             "lib": "rvl/wpad"},
    {"size_range": (60, 200), "calls": [],                     "name": "WPADGetInfo",          "lib": "rvl/wpad"},
    {"size_range": (100,300), "calls": [],                     "name": "KPADInit",             "lib": "rvl/kpad"},
    {"size_range": (100,400), "calls": [],                     "name": "KPADRead",             "lib": "rvl/kpad"},
    {"size_range": (200,500), "calls": [],                     "name": "SCInit",               "lib": "rvl/sc"},
    # ── libc / MSL ───────────────────────────────────────────────────────────
    {"size_range": (24, 48),  "calls": [],                     "name": "strlen",               "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "strcpy",               "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "strncpy",              "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "strcmp",               "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "strncmp",              "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "strcat",               "lib": "libc"},
    {"size_range": (100,300), "calls": [],                     "name": "sprintf",              "lib": "libc"},
    {"size_range": (100,300), "calls": [],                     "name": "snprintf",             "lib": "libc"},
    {"size_range": (100,300), "calls": [],                     "name": "vsprintf",             "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "memcpy",               "lib": "libc"},
    {"size_range": (40, 100), "calls": [],                     "name": "memset",               "lib": "libc"},
    {"size_range": (48, 120), "calls": [],                     "name": "memcmp",               "lib": "libc"},
    {"size_range": (40, 100), "calls": [],                     "name": "memmove",              "lib": "libc"},
    {"size_range": (200,600), "calls": [],                     "name": "malloc",               "lib": "libc"},
    {"size_range": (200,600), "calls": [],                     "name": "free",                 "lib": "libc"},
    {"size_range": (200,600), "calls": [],                     "name": "realloc",              "lib": "libc"},
    {"size_range": (40, 120), "calls": [],                     "name": "abort",                "lib": "libc"},
    {"size_range": (100,300), "calls": [],                     "name": "printf",               "lib": "libc"},
    {"size_range": (40, 120), "calls": [],                     "name": "atoi",                 "lib": "libc"},
    {"size_range": (40, 120), "calls": [],                     "name": "atof",                 "lib": "libc"},
    {"size_range": (40, 120), "calls": [],                     "name": "abs",                  "lib": "libc"},
    {"size_range": (40, 120), "calls": [],                     "name": "sqrtf",                "lib": "libc"},
    {"size_range": (20, 60),  "calls": [],                     "name": "sinf",                 "lib": "libc"},
    {"size_range": (20, 60),  "calls": [],                     "name": "cosf",                 "lib": "libc"},
    {"size_range": (40, 120), "calls": [],                     "name": "atan2f",               "lib": "libc"},
    # ── new_/delete ───────────────────────────────────────────────────────────
    {"size_range": (20, 80),  "calls": ["malloc"],             "name": "operator new",         "lib": "c++"},
    {"size_range": (20, 80),  "calls": ["malloc"],             "name": "operator new[]",       "lib": "c++"},
    {"size_range": (20, 80),  "calls": ["free"],               "name": "operator delete",      "lib": "c++"},
    {"size_range": (20, 80),  "calls": ["free"],               "name": "operator delete[]",    "lib": "c++"},
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_hints() -> dict:
    if HINTS_FILE.exists():
        with open(HINTS_FILE) as f:
            return json.load(f)
    return {}


def save_hints(hints: dict):
    HINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HINTS_FILE, "w") as f:
        json.dump(hints, f, indent=2, sort_keys=True)


def load_symbols() -> dict[str, str]:
    """Return {symbol_name: address_hex} from symbols.txt."""
    syms: dict[str, str] = {}
    if not SYMBOLS_FILE.exists():
        return syms
    for line in SYMBOLS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"^(\S+)\s*=\s*\.\w+:0x([0-9A-Fa-f]+)", line)
        if m:
            syms[m.group(1)] = m.group(2)
    return syms


def load_objdiff_units() -> list[dict]:
    if not OBJDIFF_JSON.exists():
        return []
    with open(OBJDIFF_JSON) as f:
        return json.load(f).get("units", [])


# ─── Source 1: string references ──────────────────────────────────────────────

def _extract_strings_from_asm(asm_text: str) -> list[str]:
    """Extract visible ASCII strings referenced in an assembly file."""
    # DTK format: .string "..." or .asciz "..."
    found = re.findall(r'\.(?:string|asciz|ascii)\s+"([^"]{4,80})"', asm_text)
    # Also look for lbl_ references that appear in data sections
    return [s for s in found if s.isprintable() and len(s) >= 4]


def _strings_to_name_hint(strings: list[str]) -> str | None:
    """Turn a list of strings into a probable function name hint."""
    for s in strings:
        s = s.strip()
        # Skip generic/unhelpful strings
        if re.match(r"^[0-9\s\.]+$", s):
            continue
        if len(s) < 4:
            continue
        # If it looks like a file path or module name, derive a name
        if "/" in s or ".arc" in s or ".bti" in s or ".szs" in s:
            base = Path(s).stem.replace("-", "_").replace(" ", "_")
            return f"load_{base}" if base else None
        # If it looks like a function/error message
        if re.search(r"[A-Z][a-z]{2,}", s):
            words = re.findall(r"[A-Za-z]+", s)
            if words:
                return "_".join(w[:12] for w in words[:3]).lower()
    return None


def scan_string_hints(hints: dict) -> int:
    """Scan all ASM files for string references → naming hints."""
    added = 0
    for asm_file in ASM_DIR.rglob("*.s"):
        fn_match = re.search(r"fn_([0-9A-Fa-f]{8})", asm_file.stem)
        if not fn_match:
            continue
        addr = fn_match.group(1).upper()
        key  = f"fn_{addr}"
        if key in hints and hints[key].get("confidence", 0) >= 0.9:
            continue  # already have high-confidence info

        text    = asm_file.read_text(errors="replace")
        strings = _extract_strings_from_asm(text)
        if not strings:
            continue
        name = _strings_to_name_hint(strings)
        if not name:
            continue
        hints[key] = {
            "name": name,
            "source": "string_ref",
            "confidence": 0.5,
            "strings": strings[:3],
        }
        added += 1
    return added


# ─── Source 2: matched source scanning ────────────────────────────────────────

def _extract_defined_names(c_source: str) -> list[str]:
    """Extract function names actually defined in a C/C++ source file."""
    return re.findall(
        r"^\s*(?:static\s+)?(?:[\w*]+\s+)+(\w+)\s*\([^)]*\)\s*\{",
        c_source, re.MULTILINE
    )


def _extract_called_fn_addrs(c_source: str) -> list[str]:
    """Extract fn_XXXXXXXX addresses referenced in a source file."""
    return re.findall(r"\bfn_([0-9A-Fa-f]{8})\b", c_source, re.IGNORECASE)


def scan_matched_sources(hints: dict) -> int:
    """
    For every 100%-matched source file:
    - Extract the real function names defined in it
    - Find any fn_XXXXXXXX it calls → mark those as 'called_by:<name>'
    """
    added = 0
    for src_file in SRC_DIR.rglob("*.c"):
        text = src_file.read_text(errors="replace")
        # Quick check: does it have real function definitions (not stubs)?
        if "#include <dolphin/types.h>" in text and len(text) < 80:
            continue  # stub file

        defined_names = _extract_defined_names(text)
        called_addrs  = _extract_called_fn_addrs(text)

        # Map callee → caller names
        for addr_hex in called_addrs:
            key = f"fn_{addr_hex.upper()}"
            if key in hints and hints[key].get("confidence", 0) >= 0.7:
                continue
            if defined_names:
                caller_summary = ", ".join(defined_names[:2])
                hints[key] = {
                    "name": f"called_by_{defined_names[0]}",
                    "source": "xref",
                    "confidence": 0.4,
                    "callers": defined_names[:3],
                    "caller_file": src_file.name,
                }
                added += 1

    return added


# ─── Source 3: ASM cross-reference analysis ───────────────────────────────────

def scan_callgraph(hints: dict) -> int:
    """
    Build a call graph from ASM files. If fn_A calls fn_B and fn_A has a
    name hint, annotate fn_B as 'called by fn_A'.
    """
    added = 0
    for asm_file in ASM_DIR.rglob("*.s"):
        text     = asm_file.read_text(errors="replace")
        # This file's primary function
        fn_match = re.search(r"fn_([0-9A-Fa-f]{8})", asm_file.stem)
        if not fn_match:
            continue
        this_addr = fn_match.group(1).upper()
        this_key  = f"fn_{this_addr}"
        this_hint = hints.get(this_key, {})
        this_name = this_hint.get("name", "")

        # Find all bl fn_XXXXXXXX calls in this ASM
        callees = re.findall(r"\bbl\s+fn_([0-9A-Fa-f]{8})\b", text, re.IGNORECASE)
        for callee_addr in set(callees):
            callee_key = f"fn_{callee_addr.upper()}"
            if callee_key == this_key:
                continue
            existing = hints.get(callee_key, {})
            if existing.get("confidence", 0) >= 0.6:
                continue
            if this_name:
                hints[callee_key] = {
                    "name": f"helper_of_{this_name[:20]}",
                    "source": "callgraph",
                    "confidence": 0.3,
                    "called_by": this_key,
                }
                added += 1

    return added


# ─── Source 4: size-based SDK pattern matching ────────────────────────────────

def scan_sdk_patterns(hints: dict) -> int:
    """
    Try to match functions against known Dolphin SDK signatures by size.
    Very conservative — only records low-confidence hints.
    """
    added = 0
    symbols = load_symbols()
    # Build size map from symbols.txt  (symbol → size is hard without MAP;
    # we approximate from ASM file size)
    for asm_file in ASM_DIR.rglob("*.s"):
        fn_match = re.search(r"fn_([0-9A-Fa-f]{8})", asm_file.stem)
        if not fn_match:
            continue
        addr = fn_match.group(1).upper()
        key  = f"fn_{addr}"
        if key in hints and hints[key].get("confidence", 0) >= 0.5:
            continue

        text = asm_file.read_text(errors="replace")
        # Estimate size from .size annotations in ASM
        sizes = re.findall(r"size:\s*0x([0-9A-Fa-f]+)", text)
        total = sum(int(s, 16) for s in sizes) if sizes else 0
        if total == 0:
            continue

        calls = re.findall(r"\bbl\s+(fn_[0-9A-Fa-f]+|\w+)\b", text, re.IGNORECASE)
        call_set = set(c.lower() for c in calls)

        for sig in SDK_SIGNATURES:
            lo, hi = sig["size_range"]
            if not (lo <= total <= hi):
                continue
            # Check call requirements
            if sig["calls"] and not any(c.lower() in call_set for c in sig["calls"]):
                continue
            hints[key] = {
                "name": sig["name"],
                "source": "sdk_pattern",
                "confidence": 0.25,
                "lib": sig.get("lib", ""),
                "asm_size": total,
            }
            added += 1
            break

    return added


# ─── Source 5: cross-game SDK size matching ───────────────────────────────────

def scan_cross_game_sdk(hints: dict) -> int:
    """
    Cross-reference fn_* sizes against data/sdk_signatures.json — a database of
    4277 SDK/framework functions whose sizes are confirmed across SS + ogws (+ bba-wd).
    Only emits hints for sizes that map to a single function name (unambiguous).
    Confidence: 0.75 (size alone; call-pattern check not performed here).
    """
    sdk_file = PROJECT_ROOT / "data" / "sdk_signatures.json"
    if not sdk_file.exists():
        return 0

    with open(sdk_file) as f:
        sdk_sigs = json.load(f)

    # Tiered size → name maps:
    #   Tier 1: sizes unique within official RVL SDK .a files (highest confidence)
    #   Tier 2: sizes unique across all sources (original behaviour)
    rvl_size_to_name: dict[int, str] = {}
    all_size_to_name: dict[int, str] = {}
    rvl_dups: set[int] = set()
    all_dups: set[int] = set()

    for name, info in sdk_sigs.items():
        sz   = info["size"]
        srcs = info.get("sources", [])
        is_rvl = any(s.startswith("rvlsdk_") for s in srcs)

        # all-source map
        if sz in all_size_to_name:
            all_dups.add(sz)
        else:
            all_size_to_name[sz] = name

        # rvl-only map
        if is_rvl:
            if sz in rvl_size_to_name:
                rvl_dups.add(sz)
            else:
                rvl_size_to_name[sz] = name

    for sz in rvl_dups:
        del rvl_size_to_name[sz]
    for sz in all_dups:
        del all_size_to_name[sz]

    # Merge: RVL SDK entries override all-source entries
    size_to_name: dict[int, str] = {**all_size_to_name, **rvl_size_to_name}

    USEFUL_PREFIXES = (
        "OS", "GX", "VI", "PAD", "DVD", "AX", "AI", "DSP", "SI", "EXI",
        "MTX", "MEM", "nw4r", "__ct__Q2", "__dt__Q2", "EGG",
        "NWC24", "udp_cc", "WPAD", "KPAD", "SC", "__AX", "TPL",
        "NAND", "IPC", "BTE", "ARC", "PMIC", "ESP", "RSO", "WBC",
        "USB", "CX", "CNT", "AXFX", "AXART", "THP", "FNT", "VCM",
        "KPADRead", "WPADRead", "HBM", "hidh_",
        "strlen", "memcpy", "memset", "sprintf", "snprintf", "printf",
        "malloc", "free", "sqrtf", "sinf", "cosf", "atan2f",
        # RVL SDK specific
        "GXDraw", "update_controller", "__THPRead", "__wpad",
        "DecompressRemainder", "CXSecure", "CXRead", "MIXInit",
        "KPADRead", "AXFXReverb", "hidh_search",
    )

    # Pre-parse symbols.txt once: {fn_ADDR: size_int}
    fn_sizes: dict[str, int] = {}
    _sym_re = re.compile(r"^(fn_[0-9A-Fa-f]{8})\s*=\s*\.text:(0x[0-9A-Fa-f]+);\s*//.*size:(0x[0-9A-Fa-f]+)")
    for line in SYMBOLS_FILE.read_text().splitlines():
        m2 = _sym_re.match(line)
        if m2:
            fn_sizes[m2.group(1)] = int(m2.group(3), 16)

    added = 0

    for key, sz in fn_sizes.items():
        if key in hints and hints[key].get("confidence", 0) >= 0.75:
            continue

        candidate = size_to_name.get(sz)
        if not candidate:
            continue
        if not any(candidate.startswith(p) for p in USEFUL_PREFIXES):
            continue

        sources = sdk_sigs[candidate].get("sources", [])
        hints[key] = {
            "name":         candidate,
            "source":       "sdk_size_match",
            "confidence":   0.75,
            "size":         hex(sz),
            "confirmed_by": sources,
        }
        added += 1

    return added


# ─── Source 6: JSystem sequence alignment ─────────────────────────────────────

def scan_jsystem_sequences(hints: dict) -> int:
    """
    Find JSystem library blocks inside ACCF by sliding windows of consecutive
    JSystem function sizes (from TWW GZLE01) over the ACCF fn_* address-sorted
    list.  A window with ≥70% size matches at the same positions is treated as
    a confirmed alignment.

    Requires data/sdk_sources/tww_GZLE01.txt (decomp-format symbols.txt).
    """
    tww_file = PROJECT_ROOT / "data" / "sdk_sources" / "tww_GZLE01.txt"
    if not tww_file.exists():
        return 0

    JSYS_PREFIXES = (
        "JKR", "JUT", "J3D", "J2D", "JAS", "JGad", "JMessage", "JSupport",
        "fopAc_", "fopHe_", "mDoExt_", "mDoCPd_", "dComIfG_",
    )

    sym_re = re.compile(
        r"^(\w[\w@:<>.,$\- *&]+)\s*=\s*\.(?:text|init):(0x[0-9A-Fa-f]+);"
        r"\s*//.*size:(0x[0-9A-Fa-f]+)"
    )
    fn_re = re.compile(
        r"^(fn_[0-9A-Fa-f]{8})\s*=\s*\.text:(0x[0-9A-Fa-f]+);\s*//.*size:(0x[0-9A-Fa-f]+)"
    )

    # Parse TWW in address order
    tww_syms: list[tuple[int, str, int]] = []
    for line in tww_file.read_text(errors="replace").splitlines():
        m = sym_re.match(line)
        if m:
            tww_syms.append((int(m.group(2), 16), m.group(1), int(m.group(3), 16)))
    tww_syms.sort()

    # Build contiguous JSystem runs (consecutive JSystem fns with no gap)
    runs: list[list[tuple[int, str, int]]] = []
    cur: list[tuple[int, str, int]] = []
    for addr, name, sz in tww_syms:
        is_js = any(name.startswith(p) for p in JSYS_PREFIXES)
        if is_js:
            if cur and cur[-1][0] + cur[-1][2] == addr:
                cur.append((addr, name, sz))
            else:
                if len(cur) >= 4:
                    runs.append(cur)
                cur = [(addr, name, sz)]
        else:
            if len(cur) >= 4:
                runs.append(cur)
            cur = []
    if len(cur) >= 4:
        runs.append(cur)

    if not runs:
        return 0

    # Parse ACCF fn_* in address order
    accf_fns: list[tuple[int, str, int]] = []
    for line in SYMBOLS_FILE.read_text().splitlines():
        m2 = fn_re.match(line)
        if m2:
            accf_fns.append((int(m2.group(2), 16), m2.group(1), int(m2.group(3), 16)))
    accf_fns.sort()
    accf_sizes = [sz for _, _, sz in accf_fns]
    N = len(accf_fns)

    THRESHOLD = 0.70   # require ≥70% of sizes to match exactly
    CONF      = 0.82   # emit at this confidence

    added = 0
    for run in runs:
        run_sizes = [sz for _, _, sz in run]
        R = len(run_sizes)
        best_pos, best_score = -1, 0
        for i in range(N - R + 1):
            matches = sum(1 for a, b in zip(accf_sizes[i:i+R], run_sizes) if a == b)
            if matches > best_score:
                best_score, best_pos = matches, i
        if best_pos < 0 or best_score / R < THRESHOLD:
            continue
        # Emit hints only for positions where sizes matched exactly
        for j, (tww_entry, accf_entry) in enumerate(
            zip(run, accf_fns[best_pos : best_pos + R])
        ):
            _, tww_name, tww_sz = tww_entry
            _, acc_fn,  acc_sz  = accf_entry
            if acc_sz != tww_sz:
                continue
            if hints.get(acc_fn, {}).get("confidence", 0) >= CONF:
                continue
            hints[acc_fn] = {
                "name":       tww_name,
                "source":     "jsys_sequence_match",
                "confidence": CONF,
                "evidence":   f"TWW window {best_score}/{R} match, exact size {hex(acc_sz)}",
            }
            added += 1

    return added


# ─── Apply hints to symbols.txt ───────────────────────────────────────────────

def apply_hints_to_symbols(hints: dict, min_confidence: float = 0.6):
    """
    Write high-confidence name hints as comments into symbols.txt.
    We don't rename the actual symbol (that would break the build) but
    add a comment so the LLM sees context.
    """
    if not SYMBOLS_FILE.exists():
        return

    lines   = SYMBOLS_FILE.read_text().splitlines()
    updated = []
    changes = 0

    for line in lines:
        m = re.match(r"^(fn_([0-9A-Fa-f]{8})\s*=\s*\S+.*)", line)
        if m:
            key  = f"fn_{m.group(2).upper()}"
            hint = hints.get(key, {})
            conf = hint.get("confidence", 0)
            name = hint.get("name", "")
            if conf >= min_confidence and name and "/*" not in line:
                line  = line.rstrip() + f"  /* {name} (conf={conf:.2f}) */"
                changes += 1
        updated.append(line)

    if changes:
        SYMBOLS_FILE.write_text("\n".join(updated) + "\n")
        print(f"  ✏   Added {changes} name hint(s) to symbols.txt")


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report(hints: dict):
    by_conf = sorted(hints.items(), key=lambda kv: -kv[1].get("confidence", 0))
    lines   = [f"Symbol hunter report — {len(hints)} hints\n{'='*60}"]
    for key, info in by_conf[:100]:
        name   = info.get("name", "?")
        conf   = info.get("confidence", 0)
        source = info.get("source", "?")
        lines.append(f"  {key:<20}  {conf:.2f}  [{source:<14}]  {name}")
    report = "\n".join(lines)
    print(report)
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(report + "\n")
    print(f"\n  Report saved → {REPORT_FILE.relative_to(PROJECT_ROOT)}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Automated symbol naming hunter for ACCF (RUUE01)"
    )
    parser.add_argument("--apply",  action="store_true",
                        help="Write high-confidence hints as comments in symbols.txt")
    parser.add_argument("--report", action="store_true",
                        help="Print report and exit (no scanning)")
    parser.add_argument("--min-confidence", type=float, default=0.6,
                        help="Minimum confidence to apply to symbols.txt (default 0.6)")
    args = parser.parse_args()

    hints = load_hints()

    if args.report:
        print_report(hints)
        return

    print("🔍  Scanning string references …")
    n = scan_string_hints(hints)
    print(f"    +{n} hints from string references")

    print("🔍  Scanning matched source files …")
    n = scan_matched_sources(hints)
    print(f"    +{n} hints from matched sources")

    print("🔍  Building call graph …")
    n = scan_callgraph(hints)
    print(f"    +{n} hints from call graph")

    print("🔍  Matching Dolphin SDK patterns …")
    n = scan_sdk_patterns(hints)
    print(f"    +{n} hints from SDK patterns")

    # Auto-ingest any new SDK source files dropped into data/sdk_sources/
    sdk_sources_dir = PROJECT_ROOT / "data" / "sdk_sources"
    if sdk_sources_dir.exists() and any(
        p.is_file() and p.suffix.lower() not in (".source", ".gitkeep", ".md")
        for p in sdk_sources_dir.iterdir()
    ):
        print("📦  Ingesting SDK sources from data/sdk_sources/ …")
        try:
            from ingest_sdk_sources import ingest as _ingest_sdk
            stats = _ingest_sdk(dry_run=False, verbose=False)
            print(f"    {stats['files']} file(s) → +{stats['new']} new, +{stats['updated']} updated "
                  f"({stats['total']} total, {stats['confirmed_3plus']} confirmed 3+)")
        except Exception as e:
            print(f"    [warn] ingest_sdk_sources failed: {e}")

    print("🔍  Cross-game SDK size matching (all sources) …")
    n = scan_cross_game_sdk(hints)
    print(f"    +{n} hints from cross-game SDK sizes")

    print("🔍  JSystem sequence alignment (TWW) …")
    n = scan_jsystem_sequences(hints)
    print(f"    +{n} hints from JSystem sequence matching")

    save_hints(hints)
    print(f"\n  💾  Saved {len(hints)} total hints → {HINTS_FILE.relative_to(PROJECT_ROOT)}")

    if args.apply:
        apply_hints_to_symbols(hints, min_confidence=args.min_confidence)

    print_report(hints)


if __name__ == "__main__":
    main()
