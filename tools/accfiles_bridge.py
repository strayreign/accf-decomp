#!/usr/bin/env python3
"""
accfiles_bridge.py — Bridge between the decomp pipeline and the ACCFiles
reference folder.

ACCFiles contains:
  - main.dol.c          : Full Ghidra decompilation (~879K lines)
  - wii-symbols-master/ : Cross-game Wii symbol databases
  - OTHER GAME SYMBOLS/wii_development_package/RVL_SDK/include/ : 267 official SDK headers
  - ACCF.rep/           : Ghidra project repository
  - ROMs/               : Disc images (all regions)
  - objdiff-mcp/        : objdiff MCP server scripts
  - bridge_mcp_ghidra.py: Ghidra MCP bridge

This module:
  1. Indexes the Ghidra decompilation by function address for instant lookup
  2. Serves official RVL_SDK headers (higher quality than scraped GitHub copies)
  3. Provides an LLM-driven file organizer (classify -> move redundant -> delete)
  4. Exposes get_ghidra_context() and get_sdk_header() for decomp_loop.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path resolution — works on both Windows and Linux (bash sandbox)
# ---------------------------------------------------------------------------

_ACCFILES_CANDIDATES = [
    Path(r"E:\Users\PC\dev\ACCFiles"),                              # Windows host
    Path("/sessions/sweet-great-pascal/mnt/ACCFiles"),              # bash sandbox
    Path(__file__).resolve().parent.parent.parent / "ACCFiles",     # sibling dir
]

ACCFILES_ROOT: Path | None = None
for _p in _ACCFILES_CANDIDATES:
    if _p.exists():
        ACCFILES_ROOT = _p
        break

PROJECT_ROOT   = Path(__file__).resolve().parent.parent
INDEX_DIR      = PROJECT_ROOT / "data" / "accfiles_index"


# ---------------------------------------------------------------------------
# 1. Ghidra decompilation index
# ---------------------------------------------------------------------------

_GHIDRA_INDEX: dict[str, tuple[int, int]] | None = None   # addr -> (line_start, line_end)
_GHIDRA_FILE:  Path | None = None
_GHIDRA_INDEX_FILE = INDEX_DIR / "ghidra_func_index.json"


def _find_ghidra_c() -> Path | None:
    if ACCFILES_ROOT is None:
        return None
    p = ACCFILES_ROOT / "main.dol.c"
    return p if p.exists() else None


def build_ghidra_index(force: bool = False) -> int:
    """
    Scan main.dol.c and build a {hex_addr: (start_line, end_line)} index.
    Only runs if index doesn't exist or force=True.
    Returns number of functions indexed.
    """
    global _GHIDRA_INDEX, _GHIDRA_FILE
    _GHIDRA_FILE = _find_ghidra_c()
    if _GHIDRA_FILE is None:
        return 0

    if not force and _GHIDRA_INDEX_FILE.exists():
        try:
            _GHIDRA_INDEX = json.loads(_GHIDRA_INDEX_FILE.read_text())
            return len(_GHIDRA_INDEX)
        except Exception:
            pass

    # Pattern: Ghidra outputs functions like  void FUN_80007878(void)
    func_re = re.compile(
        r"^(?:undefined\d*|void|int|uint|ushort|short|char|uchar|bool|long|ulong|float|double|pointer|"
        r"longlong|ulonglong|undefined|byte|ubyte|wchar16|"
        r"[A-Z]\w+\s*\*?)\s+FUN_([0-9a-fA-F]{8})\s*\(",
        re.IGNORECASE,
    )

    index: dict[str, tuple[int, int]] = {}
    current_addr: str | None = None
    current_start: int = 0
    brace_depth = 0

    with open(_GHIDRA_FILE, "r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            m = func_re.match(line)
            if m and brace_depth == 0:
                # Close previous function if any
                if current_addr is not None:
                    index[current_addr] = (current_start, lineno - 1)
                current_addr = m.group(1).upper()
                current_start = lineno
                brace_depth = 0

            brace_depth += line.count("{") - line.count("}")

            if current_addr and brace_depth <= 0 and lineno > current_start:
                index[current_addr] = (current_start, lineno)
                current_addr = None
                brace_depth = 0

    # Close last function
    if current_addr is not None:
        index[current_addr] = (current_start, lineno)

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _GHIDRA_INDEX_FILE.write_text(json.dumps(index))
    _GHIDRA_INDEX = index
    return len(index)


def _ensure_ghidra_index():
    global _GHIDRA_INDEX, _GHIDRA_FILE
    if _GHIDRA_INDEX is not None:
        return
    _GHIDRA_FILE = _find_ghidra_c()
    if _GHIDRA_INDEX_FILE.exists():
        try:
            _GHIDRA_INDEX = json.loads(_GHIDRA_INDEX_FILE.read_text())
            return
        except Exception:
            pass
    build_ghidra_index()


def get_ghidra_context(unit_name: str, asm_text: str, max_chars: int = 4000) -> str:
    """
    Look up functions in the Ghidra decompilation that match addresses
    referenced in this unit's assembly.  Returns the Ghidra pseudocode
    as context for the LLM.
    """
    _ensure_ghidra_index()
    if not _GHIDRA_INDEX or _GHIDRA_FILE is None or not _GHIDRA_FILE.exists():
        return ""

    # Extract addresses from the unit name and from asm references
    addrs: list[str] = []
    unit_m = re.search(r"_([0-9A-Fa-f]{8})_", unit_name)
    if unit_m:
        addrs.append(unit_m.group(1).upper())
    for m in re.finditer(r"\bFUN_([0-9A-Fa-f]{8})\b|\bfn_([0-9A-Fa-f]{8})\b", asm_text, re.I):
        addr = (m.group(1) or m.group(2)).upper()
        addrs.append(addr)

    # Deduplicate, prioritize the unit's own address first
    seen = set()
    unique_addrs = []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            unique_addrs.append(a)

    if not unique_addrs:
        return ""

    # Read matching functions from main.dol.c
    parts: list[str] = []
    total_chars = 0
    lines_cache: list[str] | None = None

    for addr in unique_addrs:
        if total_chars >= max_chars:
            break
        span = _GHIDRA_INDEX.get(addr)
        if not span:
            continue
        start, end = span
        # Lazy-load the file lines
        if lines_cache is None:
            lines_cache = _GHIDRA_FILE.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()

        func_lines = lines_cache[start - 1 : end]
        func_text = "\n".join(func_lines)

        # Truncate individual functions that are too long
        if len(func_text) > 1500:
            func_text = func_text[:1500] + "\n  // ... (truncated)"

        if total_chars + len(func_text) > max_chars:
            break
        parts.append(func_text)
        total_chars += len(func_text) + 20

    if not parts:
        return ""

    header = (
        f"=== Ghidra decompilation reference ({len(parts)} fn(s) from main.dol.c) ===\n"
        "  NOTE: Ghidra output uses its own naming (FUN_/DAT_). Translate to mwcc style.\n"
        "  Variable types/names are guesses — focus on STRUCTURE and CONTROL FLOW.\n\n"
    )
    return header + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 2. Official RVL_SDK headers (higher quality than GitHub scrapes)
# ---------------------------------------------------------------------------

_SDK_INCLUDE_DIR: Path | None = None


def _find_sdk_include() -> Path | None:
    if ACCFILES_ROOT is None:
        return None
    p = ACCFILES_ROOT / "OTHER GAME SYMBOLS" / "wii_development_package" / "RVL_SDK" / "include"
    return p if p.exists() else None


_SDK_HEADER_INDEX: dict[str, Path] | None = None


def _build_sdk_header_index() -> dict[str, Path]:
    global _SDK_HEADER_INDEX
    if _SDK_HEADER_INDEX is not None:
        return _SDK_HEADER_INDEX

    sdk = _find_sdk_include()
    if sdk is None:
        _SDK_HEADER_INDEX = {}
        return _SDK_HEADER_INDEX

    idx: dict[str, Path] = {}
    for h in sdk.rglob("*.h"):
        # Index by filename (e.g. "GXEnum.h") and by relative path
        idx[h.name.lower()] = h
        rel = str(h.relative_to(sdk)).replace("\\", "/").lower()
        idx[rel] = h
    _SDK_HEADER_INDEX = idx
    return _SDK_HEADER_INDEX


def get_sdk_header(name: str, max_lines: int = 200) -> str:
    """Return the content of an official SDK header by name."""
    idx = _build_sdk_header_index()
    path = idx.get(name.lower())
    if path is None or not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append(f"// ... ({len(lines)} lines total, truncated)")
        return f"/* === Official RVL_SDK: {path.name} === */\n" + "\n".join(lines)
    except Exception:
        return ""


def get_relevant_sdk_headers(asm_text: str, max_chars: int = 3000) -> str:
    """
    Pattern-match against the assembly to find which official SDK headers
    are relevant.  Returns concatenated header content.
    """
    idx = _build_sdk_header_index()
    if not idx:
        return ""

    asm_low = asm_text.lower()
    triggers: list[tuple[str, list[str]]] = [
        # (header_filename, [asm_patterns])
        ("dolphin/os/osthread.h",    ["osthread", "oscreatethread", "osresume", "ossuspend"]),
        ("dolphin/os/osmutex.h",     ["osmutex", "oslockmutex", "osunlockmutex"]),
        ("dolphin/os/osmessage.h",   ["osmessage", "ossendmessage", "osreceivemessage"]),
        ("dolphin/os/osalarm.h",     ["osalarm", "ossetalarm", "oscancelalarm"]),
        ("dolphin/os/osmemory.h",    ["osmemory", "osprotect"]),
        ("dolphin/os/oscache.h",     ["oscache", "dcflush", "icflush", "dcinvalidate"]),
        ("dolphin/os/osinterrupt.h", ["osinterrupt", "osdisableinterrupt", "osrestoreinterrupt"]),
        ("dolphin/gx/gxenum.h",     ["gx_", "gxset", "gxload", "gxbegin", "gxinvalidate"]),
        ("dolphin/gx/gxstruct.h",   ["gxtevstage", "gxcolor", "gxvtxdesc"]),
        ("dolphin/gx/gxpixel.h",    ["gxsetblendmode", "gxsetalpha", "gxsetzmode"]),
        ("dolphin/gx/gxtexture.h",  ["gxinittexobj", "gxloadtexobj", "gxsettexcoord"]),
        ("dolphin/gx/gxtransform.h", ["gxsetproject", "gxloadpos", "gxsetview"]),
        ("dolphin/gx/gxgeometry.h",  ["gxsetvtxdesc", "gxcleargeometry"]),
        ("dolphin/gx/gxfifo.h",     ["gxfifo", "gxsetgpfifo", "gxinitfifo"]),
        ("dolphin/gx/gxvert.h",     ["gxposition", "gxnormal", "gxcolor1"]),
        ("dolphin/gx/gxtev.h",      ["gxsettev", "gxsettevcolor", "gxsettevorder"]),
        ("dolphin/gx/gxlight.h",    ["gxinitlight", "gxloadlight", "gxsetchanctrl"]),
        ("dolphin/dvd.h",           ["dvd", "dvdread", "dvdopen", "dvdclose"]),
        ("dolphin/pad.h",           ["pad", "padread", "padclamp"]),
        ("dolphin/vi.h",            ["vi", "viconfigure", "visetblack"]),
        ("dolphin/ai.h",            ["aiinit", "aistart", "aistop"]),
        ("dolphin/ax.h",            ["ax", "axinit", "axquit", "axgetaux"]),
        ("dolphin/dsp.h",           ["dsp", "dspinit"]),
        ("dolphin/mtx.h",           ["mtx", "psmtx", "vec", "quat"]),
        ("dolphin/db.h",            ["dbinterface", "dbinit"]),
        ("revolution.h",            ["wpad", "kpad", "cnt"]),
        ("revolution/wpad.h",       ["wpad", "wpadinit", "wpadread"]),
        ("revolution/kpad.h",       ["kpad", "kpadinit", "kpadread"]),
        ("revolution/nand.h",       ["nand", "nandopen", "nandread", "nandwrite"]),
        ("revolution/sc.h",         ["scget", "scset", "scinit"]),
        ("revolution/mem.h",        ["memalloc", "memfree", "memheap"]),
        ("revolution/arc.h",        ["arcinit", "arcopen", "arcgetfile"]),
    ]

    parts: list[str] = []
    total = 0
    for hdr_key, patterns in triggers:
        if total >= max_chars:
            break
        if not any(p in asm_low for p in patterns):
            continue
        path = idx.get(hdr_key)
        if path is None or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()[:200]
            chunk = f"/* === Official RVL_SDK: {path.name} === */\n" + "\n".join(lines)
            if total + len(chunk) > max_chars:
                chunk = chunk[: max_chars - total]
            parts.append(chunk)
            total += len(chunk)
        except Exception:
            continue

    if not parts:
        return ""
    return "=== Official RVL_SDK headers (from local ACCFiles) ===\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 3. LLM-driven file organizer
# ---------------------------------------------------------------------------

# Categories for the LLM to assign:
#   "essential"    — SDK headers, symbol files, Ghidra project, decomp output
#   "reference"    — ROMs, other game symbols, dev docs (keep but not critical)
#   "redundant"    — duplicates, temp files, outdated copies, .DS_Store
#   "unknown"      — needs human review

_BUILTIN_RULES: list[tuple[str, str, str]] = [
    # (glob_pattern, category, reason)
    ("*.DS_Store",       "redundant", "macOS metadata"),
    ("**/.DS_Store",     "redundant", "macOS metadata"),
    ("**/__pycache__/**","redundant", "Python cache"),
    ("**/*.pyc",         "redundant", "Python bytecode"),

    ("main.dol.c",       "essential", "Full Ghidra decompilation — primary reference"),
    ("ACCF.gpr",         "essential", "Ghidra project file"),
    ("ACCF.rep/**",      "essential", "Ghidra repository"),
    ("bridge_mcp_ghidra.py", "essential", "Ghidra MCP bridge script"),

    ("wii-symbols-master/**", "essential", "Cross-game Wii symbol database"),

    ("OTHER GAME SYMBOLS/wii_development_package/RVL_SDK/include/**",
                         "essential", "Official RVL_SDK headers — critical for decomp"),
    ("OTHER GAME SYMBOLS/wii_development_package/RVL_SDK/man/**",
                         "reference", "SDK documentation"),
    ("OTHER GAME SYMBOLS/wii_development_package/RVL_SDK/**/*.a",
                         "reference", "Precompiled SDK libraries"),
    ("OTHER GAME SYMBOLS/wii_development_package/*.zip",
                         "reference", "SDK archives (can re-extract)"),
    ("OTHER GAME SYMBOLS/wii_development_package/*.pdf",
                         "reference", "Dev hardware manuals"),
    ("OTHER GAME SYMBOLS/Official Nintendo SDKs 2010.7z",
                         "reference", "Full SDK archive — large, keep separately"),
    ("OTHER GAME SYMBOLS/Step.rpx*",
                         "reference", "Other game binaries for cross-ref"),

    ("ROMs/*.rvz",       "reference", "Disc images — needed for extraction, large"),
    ("objdiff-mcp/**",   "reference", "objdiff MCP server scripts"),
    ("acww-hax-master.zip", "reference", "AC Wild World hacking reference"),
    ("message.txt",      "unknown",   "Unknown text file — check contents"),
]


def classify_files(accfiles_path: Path | None = None) -> dict[str, list[dict]]:
    """
    Classify all files in ACCFiles using built-in rules.
    Returns {category: [{path, reason, size_bytes}]}.
    """
    root = accfiles_path or ACCFILES_ROOT
    if root is None or not root.exists():
        return {}

    result: dict[str, list[dict]] = {
        "essential": [], "reference": [], "redundant": [], "unknown": [],
    }

    all_files = list(root.rglob("*"))
    all_files = [f for f in all_files if f.is_file()]

    classified = set()

    for pattern, category, reason in _BUILTIN_RULES:
        for f in root.glob(pattern):
            if f.is_file() and f not in classified:
                classified.add(f)
                result[category].append({
                    "path": str(f.relative_to(root)),
                    "reason": reason,
                    "size_bytes": f.stat().st_size,
                })

    # Anything not matched is "unknown"
    for f in all_files:
        if f not in classified:
            result["unknown"].append({
                "path": str(f.relative_to(root)),
                "reason": "Not matched by any rule",
                "size_bytes": f.stat().st_size,
            })

    return result


def organize_files(
    accfiles_path: Path | None = None,
    dry_run: bool = True,
    move_redundant: bool = True,
    llm_classify_unknowns: bool = False,
    llm_call: callable = None,
) -> dict:
    """
    Organize ACCFiles by moving redundant files to a 'used/' subfolder.

    Args:
        accfiles_path:  Override ACCFiles root path.
        dry_run:        If True, only report what would happen. Default True.
        move_redundant: Move files classified as 'redundant' to used/.
        llm_classify_unknowns: If True, call llm_call() to classify unknown files.
        llm_call:       Function(file_path, file_preview) -> category string.
                        Only used if llm_classify_unknowns=True.

    Returns dict with keys: moved, skipped, errors, would_move (dry_run).
    """
    root = accfiles_path or ACCFILES_ROOT
    if root is None:
        return {"error": "ACCFiles root not found"}

    classified = classify_files(root)
    used_dir = root / "used"

    report = {
        "moved": [],
        "would_move": [],
        "skipped": [],
        "errors": [],
        "llm_reclassified": [],
    }

    # Optionally have the LLM classify unknowns
    if llm_classify_unknowns and llm_call is not None:
        for entry in classified.get("unknown", []):
            fpath = root / entry["path"]
            try:
                # Read a preview for the LLM
                if fpath.suffix.lower() in (".c", ".h", ".py", ".txt", ".md", ".json", ".yml"):
                    preview = fpath.read_text(encoding="utf-8", errors="replace")[:2000]
                else:
                    preview = f"[Binary file: {fpath.suffix}, {entry['size_bytes']} bytes]"

                prompt = textwrap.dedent(f"""\
                    Classify this file from an ACCF (Animal Crossing: City Folk) decompilation
                    reference folder. Respond with EXACTLY one word:
                    essential, reference, redundant, or unknown.

                    File: {entry['path']}
                    Size: {entry['size_bytes']} bytes
                    Preview:
                    {preview[:1500]}
                """)
                category = llm_call(prompt).strip().lower()
                if category in ("essential", "reference", "redundant"):
                    entry["reason"] = f"LLM classified as {category}"
                    classified.setdefault(category, []).append(entry)
                    report["llm_reclassified"].append({
                        "path": entry["path"],
                        "new_category": category,
                    })
            except Exception as e:
                report["errors"].append({"path": entry["path"], "error": str(e)})

    # Move redundant files
    to_move = classified.get("redundant", []) if move_redundant else []
    for entry in to_move:
        src = root / entry["path"]
        dst = used_dir / entry["path"]

        if dry_run:
            report["would_move"].append(entry["path"])
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            report["moved"].append(entry["path"])
        except Exception as e:
            report["errors"].append({"path": entry["path"], "error": str(e)})

    return report


def print_inventory(accfiles_path: Path | None = None):
    """Print a human-readable inventory of ACCFiles."""
    classified = classify_files(accfiles_path)

    for category in ("essential", "reference", "redundant", "unknown"):
        items = classified.get(category, [])
        total_size = sum(i["size_bytes"] for i in items)
        size_str = _human_size(total_size)
        print(f"\n{'=' * 60}")
        print(f"  {category.upper()} ({len(items)} files, {size_str})")
        print(f"{'=' * 60}")
        for item in items:
            sz = _human_size(item["size_bytes"])
            print(f"  {sz:>8s}  {item['path']}")
            print(f"           {item['reason']}")


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f}{unit}" if unit != "B" else f"{nbytes}{unit}"
        nbytes /= 1024
    return f"{nbytes:.1f}TB"


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="ACCFiles bridge & organizer")
    sub = parser.add_subparsers(dest="cmd")

    # index
    idx_p = sub.add_parser("index", help="Build Ghidra function index")
    idx_p.add_argument("--force", action="store_true")

    # inventory
    sub.add_parser("inventory", help="Print classified file inventory")

    # organize
    org_p = sub.add_parser("organize", help="Move redundant files to used/")
    org_p.add_argument("--execute", action="store_true",
                       help="Actually move files (default is dry-run)")
    org_p.add_argument("--llm", action="store_true",
                       help="Use LLM to classify unknown files")
    org_p.add_argument("--model", default="qwen3:7b",
                       help="Ollama model for LLM classification (default: qwen3:7b)")
    org_p.add_argument("--delete-used", action="store_true",
                       help="After moving to used/, delete the used/ folder")

    # context — test the context lookup
    ctx_p = sub.add_parser("context", help="Test Ghidra context lookup for a unit")
    ctx_p.add_argument("unit", help="Unit name e.g. auto_03_80452694_text")

    args = parser.parse_args()

    if args.cmd == "index":
        count = build_ghidra_index(force=args.force)
        print(f"Indexed {count} functions from main.dol.c")

    elif args.cmd == "inventory":
        print_inventory()

    elif args.cmd == "organize":
        llm_call = None
        if args.llm:
            # Wire up Ollama for classification
            try:
                import httpx
                def _ollama_classify(prompt: str) -> str:
                    url = f"http://localhost:11434/api/generate"
                    resp = httpx.post(url, json={
                        "model": args.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 10},
                    }, timeout=30)
                    return resp.json().get("response", "unknown")
                llm_call = _ollama_classify
            except ImportError:
                print("httpx not installed — LLM classification unavailable")
                llm_call = None

        report = organize_files(
            dry_run=not args.execute,
            llm_classify_unknowns=args.llm,
            llm_call=llm_call,
        )

        if not args.execute:
            print(f"\n  DRY RUN — would move {len(report['would_move'])} file(s):")
            for p in report["would_move"]:
                print(f"    -> used/{p}")
            print(f"\n  Run with --execute to actually move files.")
        else:
            print(f"\n  Moved {len(report['moved'])} file(s) to used/")
            for p in report["moved"]:
                print(f"    {p}")
            if report["errors"]:
                print(f"\n  Errors: {len(report['errors'])}")
                for e in report["errors"]:
                    print(f"    {e['path']}: {e['error']}")

            if args.delete_used and ACCFILES_ROOT:
                used_dir = ACCFILES_ROOT / "used"
                if used_dir.exists():
                    shutil.rmtree(used_dir)
                    print(f"\n  Deleted used/ folder")

        if report.get("llm_reclassified"):
            print(f"\n  LLM reclassified {len(report['llm_reclassified'])} file(s):")
            for r in report["llm_reclassified"]:
                print(f"    {r['path']} -> {r['new_category']}")

    elif args.cmd == "context":
        count = build_ghidra_index()
        print(f"Index: {count} functions")
        # Fake some asm text with addresses from the unit name
        addr_m = re.search(r"_([0-9A-Fa-f]{8})_", args.unit)
        fake_asm = f"fn_{addr_m.group(1)}" if addr_m else ""
        ctx = get_ghidra_context(args.unit, fake_asm)
        if ctx:
            print(ctx[:3000])
        else:
            print("No Ghidra context found for this unit.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
