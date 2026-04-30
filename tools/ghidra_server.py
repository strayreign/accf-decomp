#!/usr/bin/env python3
"""
ghidra_server.py — Headless GhidraMCP-compatible HTTP server for ACCF.

Uses PyGhidra (bundled with Ghidra.app) to open the accf project and serve
the same REST endpoints that ghidra_sync.py and the mcp__ghidra__* tools expect.

Usage (run in a dedicated terminal, leave it open):
    ~/GhidraMacOS/ghidra_install/Ghidra.app/Contents/Resources/ghidra/support/pyghidraRun \
        ~/Desktop/accf-decomp/tools/ghidra_server.py

Endpoints (all GhidraMCP-compatible):
    GET  /list_functions          → [{name, address}, ...]
    GET  /decompile?name=FN       → {decompiled: "..."}
    GET  /decompile_function?address=0x80001234 → {decompiled: "..."}
    GET  /get_function_by_name?name=FN → {name, address, size}
    POST /rename_function         → JSON {old_name, new_name}
    GET  /list_methods?class=X    → [{name, address}, ...]
    GET  /list_classes            → [className, ...]
    GET  /search_functions_by_name?query=X → [{name, address}, ...]

Port: 8080 (matches GhidraMCP default)
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import os
import sys

# ── Locate Ghidra install ─────────────────────────────────────────────────────
# Priority: GHIDRA_INSTALL_DIR env var → well-known portable paths → give up.
def _find_ghidra_install() -> Path:
    if "GHIDRA_INSTALL_DIR" in os.environ:
        return Path(os.environ["GHIDRA_INSTALL_DIR"])
    home = Path.home()
    if sys.platform == "win32":
        candidates = [
            Path(r"E:\Users\PC\dev\ghidra_12.0.4_PUBLIC"),
            Path(r"E:\Users\PC\dev\ghidra_12.0_PUBLIC"),
            home / "dev" / "ghidra_12.0.4_PUBLIC",
            home / "dev" / "ghidra_12.0_PUBLIC",
            Path(r"C:\ghidra_12.0.4_PUBLIC"),
        ]
    else:
        candidates = [
            home / "GhidraMacOS/ghidra_install/Ghidra.app/Contents/Resources/ghidra",
            home / "ghidra_12.0.4_PUBLIC",
            home / "ghidra_12.0_PUBLIC",
        ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        "Ghidra install not found. Set GHIDRA_INSTALL_DIR or place Ghidra in a standard location."
    )

# ── Locate project root ───────────────────────────────────────────────────────
# ghidra_server.py lives in <project_root>/tools/
_TOOLS_DIR   = Path(__file__).resolve().parent
_PROJECT_ROOT = _TOOLS_DIR.parent

GHIDRA_INSTALL = _find_ghidra_install()
GHIDRA_PROJECT = _PROJECT_ROOT / "ghidra_projects"
PROJECT_NAME   = "accf"
PORT           = 8080

# Set install dir before importing pyghidra so it doesn't need the env var
os.environ.setdefault("GHIDRA_INSTALL_DIR", str(GHIDRA_INSTALL))

# Suppress AWT GUI — headless flag works on all platforms;
# apple.awt.UIElement is macOS-only but harmless on Windows.
_java_opts = os.environ.get("_JAVA_OPTIONS", "")
if "-Djava.awt.headless=true" not in _java_opts:
    _java_opts = (_java_opts + " -Djava.awt.headless=true").strip()
if sys.platform == "darwin" and "apple.awt.UIElement" not in _java_opts:
    _java_opts = (_java_opts + " -Dapple.awt.UIElement=true").strip()
os.environ["_JAVA_OPTIONS"] = _java_opts

import pyghidra

# ── Global state (set once PyGhidra opens the program) ────────────────────────

_program   = None
_flat_api  = None
_decomp    = None   # DecompInterface


def _init_decompiler():
    global _decomp
    from ghidra.app.decompiler import DecompInterface, DecompileOptions
    opts = DecompileOptions()
    _decomp = DecompInterface()
    _decomp.setOptions(opts)
    _decomp.openProgram(_program)
    print("  Decompiler interface ready")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _decompile_function(func):
    """Return pseudo-C string for a ghidra.program.model.listing.Function."""
    from ghidra.util.task import ConsoleTaskMonitor
    monitor = ConsoleTaskMonitor()
    result  = _decomp.decompileFunction(func, 30, monitor)
    if result and result.decompileCompleted():
        return result.getDecompiledFunction().getC()
    return ""


def _fn_to_dict(func):
    return {
        "name":    func.getName(),
        "address": str(func.getEntryPoint()),
    }


def _get_function_by_name(name: str):
    funcs = list(_flat_api.getGlobalFunctions(name))
    return funcs[0] if funcs else None


def _get_function_by_address(addr_str: str):
    try:
        addr = _flat_api.toAddr(addr_str)
        return _flat_api.getFunctionAt(addr)
    except Exception:
        return None


# ── Request handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access log noise

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, qs = self._parse()

        # ── list_functions ────────────────────────────────────────────────────
        if path == "/list_functions":
            fns = _flat_api.getCurrentProgram().getFunctionManager().getFunctions(True)
            result = [_fn_to_dict(f) for f in fns]
            return self._send_json(result)

        # ── decompile (by name) ───────────────────────────────────────────────
        if path in ("/decompile", "/decompile_function"):
            name    = (qs.get("name",    [""])[0] or
                       qs.get("address", [""])[0])
            func = (_get_function_by_address(name)
                    if re.match(r'^(0x)?[0-9A-Fa-f]+$', name)
                    else _get_function_by_name(name))
            if not func:
                return self._send_json({"error": f"function not found: {name}"}, 404)
            return self._send_json({"decompiled": _decompile_function(func)})

        # ── get_function_by_name ──────────────────────────────────────────────
        if path == "/get_function_by_name":
            name = qs.get("name", [""])[0]
            func = _get_function_by_name(name)
            if not func:
                return self._send_json({"error": f"not found: {name}"}, 404)
            return self._send_json({
                "name":    func.getName(),
                "address": str(func.getEntryPoint()),
                "size":    func.getBody().getNumAddresses(),
            })

        # ── search_functions_by_name ──────────────────────────────────────────
        if path == "/search_functions_by_name":
            query = qs.get("query", [""])[0].lower()
            fns   = _flat_api.getCurrentProgram().getFunctionManager().getFunctions(True)
            result = [_fn_to_dict(f) for f in fns if query in f.getName().lower()]
            return self._send_json(result)

        # ── list_classes ──────────────────────────────────────────────────────
        if path == "/list_classes":
            sm     = _flat_api.getCurrentProgram().getSymbolTable()
            ns_mgr = _flat_api.getCurrentProgram().getNamespaceManager()
            classes = [str(ns.getName()) for ns in ns_mgr.getNamespacesDefinedWithinAddressSet(
                _flat_api.getCurrentProgram().getMemory(), True)]
            return self._send_json(classes)

        # ── list_methods ──────────────────────────────────────────────────────
        if path == "/list_methods":
            cls_name = qs.get("class", [""])[0]
            fns = _flat_api.getCurrentProgram().getFunctionManager().getFunctions(True)
            result = [_fn_to_dict(f) for f in fns
                      if f.getParentNamespace().getName() == cls_name]
            return self._send_json(result)

        self._send_json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        path, _ = self._parse()
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length)) if length else {}

        # ── rename_function ───────────────────────────────────────────────────
        if path == "/rename_function":
            old = body.get("old_name", "")
            new = body.get("new_name", "")
            func = _get_function_by_name(old)
            if not func:
                return self._send_json({"error": f"not found: {old}"}, 404)
            try:
                from ghidra.program.model.symbol import SourceType
                tx = _program.startTransaction("rename")
                func.setName(new, SourceType.USER_DEFINED)
                _program.endTransaction(tx, True)
                return self._send_json({"ok": True, "renamed": f"{old} → {new}"})
            except Exception as e:
                return self._send_json({"error": str(e)}, 500)

        self._send_json({"error": "unknown endpoint"}, 404)


# ── Entry point (called by pyghidraRun) ────────────────────────────────────────

def main():
    import traceback

    print(f"Starting headless Ghidra ({GHIDRA_INSTALL}) ...")

    try:
        # Step 1: start JVM headlessly (no GUI, no dock icon)
        print("  Starting JVM …")
        pyghidra.start(install_dir=GHIDRA_INSTALL)
        print("  JVM ready")

        # Step 2: open the existing Ghidra project (DOL was imported by analyzeHeadless)
        print(f"  Opening project {PROJECT_NAME} …")
        project = pyghidra.open_project(str(GHIDRA_PROJECT), PROJECT_NAME)
        print(f"  Project opened: {project}")

        # Step 3: find the program inside the project
        # Try common name variants Ghidra uses when importing DOL files
        from ghidra.program.flatapi import FlatProgramAPI  # type: ignore
        program = None
        consumer = None
        for prog_path in ("/main.dol", "/main", "/RUUE01", "/accf"):
            try:
                program, consumer = pyghidra.consume_program(project, prog_path)
                print(f"  Found program at project path: {prog_path}")
                break
            except FileNotFoundError:
                continue

        if program is None:
            # List what's actually in the project so we know the right path
            from ghidra.framework.model import ProjectDataUtils
            root = project.getProjectData().getRootFolder()
            files = list(root.getFiles())
            names = [str(f.getName()) for f in files]
            print(f"  ✖  Could not find program. Files in project root: {names}")
            project.close()
            return

        # Step 4: wrap in FlatProgramAPI (same interface the rest of the code uses)
        global _program, _flat_api
        _flat_api = FlatProgramAPI(program)
        _program  = program
        print(f"  Loaded: {_program.getName()}")

        _init_decompiler()

        server = HTTPServer(("127.0.0.1", PORT), Handler)
        print(f"  GhidraMCP-compat server started on port {PORT}")
        print("  Press Ctrl+C to stop.\n")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Shutting down.")
            try:
                _decomp.closeProgram()
            except Exception:
                pass
        finally:
            if consumer is not None:
                program.release(consumer)
            project.close()

    except Exception as e:
        msg = str(e)
        print(f"\n  ✖  Failed: {msg}")
        if "LanguageNotFoundException" in msg and "Gekko_Broadway" in msg:
            print()
            print("  >>> Missing Ghidra processor extension for Wii/GameCube.")
            print("  >>> Install GhidraGameCubeLoader:")
            print("  >>>   https://github.com/Cuyler36/Ghidra-GameCube-Loader/releases")
            print("  >>> Then: Ghidra GUI → File → Install Extensions → + → pick zip → restart")
        else:
            traceback.print_exc()


if __name__ == "__main__":
    main()
