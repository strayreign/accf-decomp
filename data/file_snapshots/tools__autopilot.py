#!/usr/bin/env python3
"""
autopilot.py — Fully autonomous ACCF decompilation pipeline.

Discovers EVERY unit in objdiff.json (all 634 split objects), works through
them highest-match-first, escalates models across runs via history, and loops
until everything is 100% or all models are exhausted.

Just run once and leave it:
  python3 tools/autopilot.py
  python3 tools/autopilot.py --max-attempts 6
  python3 tools/autopilot.py --min-pct 50     # tackle ≥50% first
"""

import argparse
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
try:
    from tools.self_healer import report_incident as _report_incident, record_match as _record_match
except ImportError:
    def _report_incident(*a, **kw): pass  # self_healer not available
    def _record_match(*a, **kw): pass

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OBJDIFF_JSON = PROJECT_ROOT / "objdiff.json"
REPORT_JSON  = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
OBJDIFF_CLI  = PROJECT_ROOT / "build" / "tools" / "objdiff-cli"
LOOP_SCRIPT  = PROJECT_ROOT / "tools" / "decomp_loop.py"
HISTORY_FILE = PROJECT_ROOT / "tools" / "model_history.json"
LOGS_DIR     = PROJECT_ROOT / "logs"
GITHUB_REPO  = "strayreign/accf-decomp"


# ─── Load model ladder from decomp_loop ───────────────────────────────────────

def load_model_ids() -> list[str]:
    spec = importlib.util.spec_from_file_location("decomp_loop", LOOP_SCRIPT)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return [m["id"] for m in mod.MODELS]


# ─── GitHub Actions health check + cleanup ────────────────────────────────────

def get_ci_status() -> tuple[str, str]:
    """Returns (status, conclusion) of the latest Actions run. ('', '') on error."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return "", ""
    try:
        import httpx
        with httpx.Client() as client:
            resp = client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
                params={"per_page": 1},
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return "", ""
            runs = resp.json().get("workflow_runs", [])
            if not runs:
                return "", ""
            r = runs[0]
            return r.get("status", ""), r.get("conclusion") or ""
    except Exception:
        return "", ""


def find_broken_sources() -> list[str]:
    """
    Try building all current source files with ninja and return unit names
    that fail to compile. Fast — ninja only rebuilds stale objects.
    """
    r = subprocess.run(["ninja"], cwd=PROJECT_ROOT,
                       capture_output=True, text=True)
    if r.returncode == 0:
        return []
    broken = []
    for line in r.stdout.splitlines() + r.stderr.splitlines():
        m = re.search(r"src[/\\](auto_\S+?)\.c", line)
        if m:
            name = m.group(1)
            if name not in broken:
                broken.append(name)
    return broken


def _authed_remote() -> str:
    """Return the push URL with GITHUB_TOKEN embedded for HTTPS auth."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return f"https://{token}@github.com/{GITHUB_REPO}.git"
    return f"https://github.com/{GITHUB_REPO}.git"


def initial_push_and_ci_check():
    """
    On startup: squash history to one commit, force-push, delete old Actions
    runs, then wait for the new CI run to complete before doing any work.
    Skips gracefully if GITHUB_TOKEN is not set or git push fails.
    """
    token = os.environ.get("GITHUB_TOKEN")
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME":     "strayreign",
        "GIT_AUTHOR_EMAIL":    "strayreign@users.noreply.github.com",
        "GIT_COMMITTER_NAME":  "strayreign",
        "GIT_COMMITTER_EMAIL": "strayreign@users.noreply.github.com",
    }

    def run(args, **kw):
        return subprocess.run(args, cwd=PROJECT_ROOT, env=git_env,
                              capture_output=True, text=True, **kw)

    print("  📝  Startup: squashing history → single commit …")

    # Grab last commit message BEFORE squashing so we can reuse it
    last_msg_r = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    last_msg = last_msg_r.stdout.strip() or "Initial Commit"

    # Check if there's anything to commit before touching git at all
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print("  ℹ   Nothing changed since last push — skipping startup commit")
        return

    # Build orphan branch, commit everything, replace main
    try:
        run(["git", "checkout", "--orphan", "_startup_squash"])
        run(["git", "add", "-A"])
        r = run(["git", "commit", "-m", last_msg])
        if r.returncode != 0:
            # Nothing to commit (tree unchanged) — just delete the orphan
            run(["git", "checkout", "main"])
            run(["git", "branch", "-D", "_startup_squash"])
            print("  ℹ   Nothing new to commit — skipping initial push")
            return
        run(["git", "branch", "-D", "main"])
        run(["git", "branch", "-m", "main"])
    except Exception as e:
        print(f"  ⚠  Startup squash failed: {e}")
        # Try to recover back to main
        subprocess.run(["git", "checkout", "main"], cwd=PROJECT_ROOT,
                       capture_output=True)
        return

    push_time = time.time()
    push = subprocess.run(
        ["git", "push", "--force", _authed_remote(), "main"],
        cwd=PROJECT_ROOT, env=git_env, capture_output=True, text=True, timeout=60,
    )
    if push.returncode != 0:
        print(f"  ⚠  Startup push failed: {push.stderr.strip()[:300]}")
        return

    print("  🚀  Pushed — deleting old Actions runs …")
    # Give GitHub a moment to register the new run before we delete old ones
    time.sleep(3)
    delete_old_actions()

    if not token:
        print("  ℹ   No GITHUB_TOKEN — skipping CI wait")
        return

    # Wait for CI
    monitor = PROJECT_ROOT / "tools" / "monitor.py"
    if monitor.exists():
        print("  ⏳  Waiting for CI result …")
        r = subprocess.run(
            [sys.executable, str(monitor), "--pushed-at", str(push_time)],
            cwd=PROJECT_ROOT,
        )
        if r.returncode != 0:
            print("  ❌  CI failed on startup push — will scan for broken files before working")


def delete_old_actions(keep_run_id: int | None = None):
    """
    Delete all workflow runs except the latest (or keep_run_id if given).
    Paginates through all runs. Skips in-progress/queued runs (can't delete
    them via the API), but catches everything completed.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    try:
        import httpx
    except ImportError:
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    with httpx.Client() as client:
        # Fetch the latest run id if not supplied
        if keep_run_id is None:
            resp = client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
                params={"per_page": 1}, headers=headers, timeout=15,
            )
            if resp.status_code != 200:
                return
            runs = resp.json().get("workflow_runs", [])
            if not runs:
                return
            keep_run_id = runs[0]["id"]

        deleted = 0
        page    = 1
        while True:
            resp = client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
                params={"per_page": 100, "page": page}, headers=headers, timeout=15,
            )
            if resp.status_code != 200:
                break
            runs = resp.json().get("workflow_runs", [])
            if not runs:
                break
            for run in runs:
                rid = run["id"]
                if rid == keep_run_id:
                    continue
                # Can only delete completed runs
                if run.get("status") not in ("completed",):
                    continue
                r = client.delete(
                    f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{rid}",
                    headers=headers, timeout=15,
                )
                if r.status_code in (204, 404):
                    deleted += 1
            if len(runs) < 100:
                break
            page += 1

        if deleted:
            print(f"  🗑   Deleted {deleted} old Actions run(s)\n")


# ─── build.ninja refresh ──────────────────────────────────────────────────────

def refresh_build_ninja():
    import re as _re
    ninja_file = PROJECT_ROOT / "build.ninja"
    print("  🥷  Refreshing build.ninja ...")
    r = subprocess.run([sys.executable, "configure.py"],
                       cwd=PROJECT_ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ⚠  configure.py: {r.stderr[:200]}")
    if ninja_file.exists():
        text = ninja_file.read_text()
        # Fix python interpreter
        _py = sys.executable  # lambda avoids re interpreting \U, \P etc as escapes
        patched = _re.sub(r'^python\s*=.*$', lambda m: f'python = {_py}',
                          text, flags=_re.MULTILINE)
        # Strip the entire "# Reconfigure on change" section so ninja never
        # tries to auto-regenerate build.ninja via the configure rule.
        patched = _re.sub(
            r'# Reconfigure on change\n'
            r'rule configure\n'
            r'(?:[ \t]+[^\n]*\n)*'
            r'build build\.ninja[^\n]*\n'
            r'(?:[ \t]+[^\n]*\n)*'
            r'\n?',
            '', patched, flags=_re.MULTILINE)
        if patched != text:
            ninja_file.write_text(patched)
    print("  ✅  build.ninja ready\n")


# ─── Cross-game symbols (NL RTTI + Amiibo Festival) ──────────────────────────

def run_cross_game_symbols():
    """
    Update symbol hints from cross-game RTTI data (New Leaf, Amiibo Festival).
    Rebuilds data/ac_class_context.txt used by decomp_loop.py for LLM context.
    No-op if data/nl_symbols.json doesn't exist.
    """
    script = PROJECT_ROOT / "tools" / "cross_game_symbols.py"
    if not script.exists():
        return
    r = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    for line in (r.stdout or "").splitlines():
        if line.strip() and any(k in line for k in ("✅", "📝", "hint", "Added")):
            print(f"  {line.strip()}")


# ─── Symbol hunter ────────────────────────────────────────────────────────────

def run_symbol_hunter():
    """Run symbol_hunter.py to refresh inferred symbol names."""
    hunter = PROJECT_ROOT / "tools" / "symbol_hunter.py"
    if not hunter.exists():
        return
    print("  🔍  Running symbol hunter …")
    r = subprocess.run(
        [sys.executable, str(hunter)],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if "hints" in line.lower() or "saved" in line.lower():
                print(f"  {line.strip()}")
    print()


# ─── Ghidra headless launcher ─────────────────────────────────────────────────

def _ghidra_venv_python() -> Path:
    """
    Return the Python interpreter that has pyghidra installed.
    On Windows, pyghidra is installed into the system Python via setup_win.ps1
    (pip install <ghidra>/Ghidra/Features/PyGhidra/pypkg/), so sys.executable
    is correct once setup has run.  We verify by attempting a quick import.
    On macOS/Linux a dedicated venv is typically used instead.
    Returns a non-existent sentinel path when pyghidra is not found, so
    ensure_ghidra_running() prints one quiet warning and skips cleanly.
    """
    import sys as _sys, subprocess as _sp
    current = Path(_sys.executable)

    # Fast path: if pyghidra is already importable in this process, use current interp.
    try:
        import importlib.util as _ilu
        if _ilu.find_spec("pyghidra") is not None:
            return current
    except Exception:
        pass

    # Also try a subprocess check (covers the case where we're in a different venv).
    try:
        r = _sp.run(
            [str(current), "-c", "import pyghidra"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            return current
    except Exception:
        pass

    # macOS/Linux: check dedicated Ghidra venv locations.
    if _sys.platform != "win32":
        home = Path.home()
        for c in [
            home / "Library" / "ghidra" / "ghidra_12.0_PUBLIC" / "venv" / "bin" / "python3",
            home / ".ghidra" / "ghidra_12.0_PUBLIC" / "venv" / "bin" / "python3",
        ]:
            if c.exists():
                return c

    # Nothing found — return sentinel so ensure_ghidra_running skips cleanly.
    return Path("pyghidra-not-installed")

GHIDRA_VENV_PYTHON = _ghidra_venv_python()
GHIDRA_SERVER  = PROJECT_ROOT / "tools" / "ghidra_server.py"
GHIDRA_PROJECTS = PROJECT_ROOT / "ghidra_projects"
GHIDRA_PROJECT_NAME = "accf"
GHIDRA_DOL = PROJECT_ROOT / "orig" / "RUUE01" / "main.dol"
GHIDRA_PORT = 8080

_ghidra_proc: subprocess.Popen | None = None

# ── IDA Pro constants ──────────────────────────────────────────────────────────
_IDA_DEFAULT_DIR = Path(r"C:\Program Files\IDA Professional 9.0")
IDA_DIR      = Path(os.environ.get("IDA_DIR", str(_IDA_DEFAULT_DIR)))
IDA_EXE      = IDA_DIR / "idat.exe"       # 32-bit headless (matches PPC32 target)
IDA_SERVER   = PROJECT_ROOT / "tools" / "ida_server.py"
IDA_PROJECTS = PROJECT_ROOT / "ida_projects"
IDA_DATABASE = IDA_PROJECTS / "main.dol.id0"  # first file IDA creates for the DOL db
IDA_DOL      = PROJECT_ROOT / "orig" / "RUUE01" / "main.dol"
IDA_PORT     = 8081

IDA_RECOVERY = PROJECT_ROOT / "data" / "ida_recovery.json"

# Crash signature table: Windows exit code -> recovery action.
# ensure_ida_running() matches the exit code, writes the action to
# IDA_RECOVERY, and retries.  ida_server.py reads the file at startup
# and self-heals without any manual intervention.
_IDA_CRASH_ACTIONS = {
    3221225477:  "disable_auto_analysis",  # 0xC0000005 ACCESS_VIOLATION
    3221225725:  "disable_auto_analysis",  # 0xC00000FD STACK_OVERFLOW
    3221226505:  "disable_auto_analysis",  # 0xC0000409 STACK_BUFFER_OVERRUN
    -1073741819: "disable_auto_analysis",  # signed 0xC0000005
    -1073741571: "disable_auto_analysis",  # signed 0xC00000FD
}


def _write_ida_recovery(action):
    # type: (str) -> None
    # Persist a recovery action so ida_server.py applies it on next startup.
    try:
        IDA_RECOVERY.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if IDA_RECOVERY.exists():
            try:
                import json as _rj
                existing = _rj.loads(IDA_RECOVERY.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing[action] = True
        import json as _rj2
        IDA_RECOVERY.write_text(_rj2.dumps(existing, indent=2), encoding="utf-8")
    except Exception as _we:
        print("  WARNING: could not write IDA recovery flag:", _we)


_ida_proc: subprocess.Popen | None = None


def _ghidra_port_open() -> bool:
    """Return True if something is already listening on GHIDRA_PORT."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", GHIDRA_PORT)) == 0


def ensure_ghidra_running() -> bool:
    """
    Start Ghidra headless in the background if it isn't already up.
    Returns True if Ghidra is (or becomes) reachable on port 8080.
    Skips silently if analyzeHeadless isn't installed.
    """
    global _ghidra_proc

    if _ghidra_port_open():
        return True   # already running (maybe from a previous session)

    if not GHIDRA_VENV_PYTHON.exists():
        print("  ⚠️   pyghidra not installed — run setup_win.ps1 to enable Ghidra integration")
        return False

    project_exists = (GHIDRA_PROJECTS / f"{GHIDRA_PROJECT_NAME}.rep").exists() or \
                     (GHIDRA_PROJECTS / f"{GHIDRA_PROJECT_NAME}.gpr").exists()

    if not project_exists:
        print("  ⚠️   Ghidra project not yet created — run analyzeHeadless --import first")
        return False

    GHIDRA_PROJECTS.mkdir(parents=True, exist_ok=True)

    cmd = [str(GHIDRA_VENV_PYTHON), str(GHIDRA_SERVER)]
    log = PROJECT_ROOT / "logs" / "ghidra_server.log"
    log.parent.mkdir(exist_ok=True)

    print("  🐉  Starting Ghidra headless ...")
    _ghidra_proc = subprocess.Popen(
        cmd,
        stdout=open(log, "w"),
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
    )

    # Register cleanup so Ghidra dies when autopilot exits
    def _kill_ghidra(*_):
        if _ghidra_proc and _ghidra_proc.poll() is None:
            _ghidra_proc.terminate()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT,  _kill_ghidra)
        signal.signal(signal.SIGTERM, _kill_ghidra)

    # Wait up to 60 s for the HTTP server to come up
    for i in range(60):
        time.sleep(1)
        if _ghidra_port_open():
            print(f"  ✅  Ghidra MCP ready on port {GHIDRA_PORT} (took {i+1}s)")
            return True
        if _ghidra_proc.poll() is not None:
            print(f"  ✖   Ghidra process died — check {log}")
            return False
        if i % 10 == 9:
            print(f"  ⏳  Waiting for Ghidra … ({i+1}s)")

    print(f"  ⚠️   Ghidra didn't start in 60s — continuing without it (check {log})")
    return False


# ─── Ghidra sync ──────────────────────────────────────────────────────────────

def run_ghidra_sync():
    """
    Sync named functions from GhidraMCP into symbol_hints.json.
    Exits cleanly if Ghidra is not running.
    """
    sync = PROJECT_ROOT / "tools" / "ghidra_sync.py"
    if not sync.exists():
        return
    print("  🐉  Syncing Ghidra symbols ...")
    r = subprocess.run(
        [sys.executable, str(sync)],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    for line in (r.stdout or "").splitlines():
        if any(k in line.lower() for k in ("hint", "connect", "skip", "saved", "+")):
            print(f"  {line.strip()}")
    print()


# ─── IDA Pro headless ─────────────────────────────────────────────────────────

def _ida_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", IDA_PORT)) == 0


def _ida_exe() -> Path | None:
    """Return idat.exe or idat64.exe, whichever exists in IDA_DIR."""
    for name in ("idat.exe", "idat64.exe"):
        p = IDA_DIR / name
        if p.exists():
            return p
    return None


def _kill_existing_ida(wait_port_closed: bool = True):
    """
    Kill any idat.exe / idat64.exe processes and wait until port 8081 is free.
    Safe to call even if nothing is running.
    """
    import subprocess as _sp
    for exe_name in ("idat.exe", "idat64.exe"):
        try:
            _sp.run(["taskkill", "/F", "/IM", exe_name],
                    capture_output=True, timeout=10)
        except Exception:
            pass
    if not wait_port_closed:
        time.sleep(1)
        return
    # Poll until port 8081 is actually closed (up to 15 s)
    for _ in range(15):
        time.sleep(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.settimeout(0.3)
            if _s.connect_ex(("127.0.0.1", IDA_PORT)) != 0:
                return   # port is closed — safe to start a new process
    # Still open after 15 s — taskkill may have failed (permissions?)
    print(f"  ⚠️   Port {IDA_PORT} still open after kill attempt — new IDA may fail to bind")


def ensure_ida_running() -> bool:
    """
    Start IDA Pro headless in the background if not already up.
    On first run IDA creates ida_projects/main.idb from the DOL (slow ~2-5 min).
    Subsequent runs reopen the existing database (fast ~10-30s).
    Returns True when port 8081 is reachable.
    """
    global _ida_proc

    if _ida_port_open():
        # Port is open — but validate it actually has code.
        # A stale idat.exe from a previous session with a bad (COFF-misparse)
        # database will respond on port 8081 but return 0 functions.  In that
        # case kill it and fall through to start a clean first-run below.
        try:
            import httpx as _hx
            r = _hx.get(f"http://localhost:{IDA_PORT}/list_functions", timeout=5.0)
            if r.status_code == 200:
                funcs = r.json()
                if isinstance(funcs, list) and len(funcs) > 0:
                    print(f"  ✅  IDA already running — {len(funcs)} functions loaded")
                    return True
        except Exception:
            pass
        # Either 0 functions or couldn't reach — kill and restart fresh
        print("  🔄  Stale IDA instance detected (0 functions) — killing and restarting ...")
        _kill_existing_ida()
        # Wipe the bad database so IDA rebuilds from scratch
        for _ext in (".id0", ".id1", ".nam", ".til", ".idb"):
            _p = IDA_PROJECTS / f"main.dol{_ext}"
            if _p.exists():
                try:
                    _p.unlink()
                except Exception:
                    pass

    exe = _ida_exe()
    if exe is None:
        print(f"  ⚠️   IDA not found in {IDA_DIR} — set IDA_DIR env var to override")
        return False

    if not IDA_DOL.exists():
        print(f"  ⚠️   DOL not found at {IDA_DOL} — skipping IDA")
        return False

    IDA_PROJECTS.mkdir(parents=True, exist_ok=True)
    log = PROJECT_ROOT / "logs" / "ida_server.log"
    log.parent.mkdir(exist_ok=True)

    first_run = not IDA_DATABASE.exists()

    # If the DOL loader was installed (or updated) after the database was created,
    # the old db was analysed without the loader (empty / COFF misparse).  Wipe it
    # and let IDA rebuild from scratch with the loader present.
    loader_dst = IDA_DIR / "loaders" / "dol_loader.py"
    if not first_run and loader_dst.exists() and IDA_DATABASE.exists():
        if loader_dst.stat().st_mtime > IDA_DATABASE.stat().st_mtime:
            print("  🔄  DOL loader newer than IDA database — wiping stale db for clean re-analysis ...")
            import shutil as _su
            for _ext in (".id0", ".id1", ".nam", ".til", ".idb"):
                _p = IDA_PROJECTS / f"main.dol{_ext}"
                if _p.exists():
                    _p.unlink()
            first_run = True

    # -S must be an ABSOLUTE path (no backslash issues); use forward slashes.
    script_arg = f"-S{IDA_SERVER.resolve().as_posix()}"

    # -A = autonomous (no GUI/dialogs)
    # -c = create new database (first run only)
    # NO -pppc: the DOL loader's accept_file() sets the processor to 'ppc'.
    #           Passing -pppc pre-sets the processor before loader detection runs
    #           and can interfere with the loader being selected in IDA 9.
    # -L = IDA's own log path (no space after flag)
    # -S = IDAPython script to run; use as_posix() so IDA gets forward-slash path
    staged_dol  = IDA_PROJECTS / IDA_DOL.name   # ida_projects/main.dol
    log_flag    = f"-L{log.resolve()}"
    script_flag = f"-S{IDA_SERVER.resolve().as_posix()}"
    # Capture IDAPython stdout/stderr to a separate file so we can see loader
    # print statements, tracebacks, and analysis progress.
    ida_out_log = PROJECT_ROOT / "logs" / "ida_output.log"

    if first_run:
        # Stage DOL into ida_projects/ so IDA creates the database there
        import shutil as _shutil
        if not staged_dol.exists():
            _shutil.copy2(IDA_DOL, staged_dol)
        cmd = [
            str(exe),
            "-A",
            "-c",
            "-pppc",        # force PowerPC BE 32 before any analysis runs
            log_flag,
            script_flag,
            str(staged_dol),
        ]
        print("  🔬  IDA first run — analysing DOL (2-5 min, one time only) ...")
    else:
        # Re-open existing database: pass the staged DOL path (no -c flag).
        # IDA finds the associated .id0/.id1/.nam/.til files automatically.
        cmd = [
            str(exe),
            "-A",
            log_flag,
            script_flag,
            str(staged_dol),
        ]
        print(f"  🔬  Starting IDA headless (reopening database) ...")

    if sys.platform == "win32":
        # Launch IDA inside a PowerShell window that tails its log so the
        # user can see real-time output without it hijacking the main console.
        _ida_log_path = str(log.resolve()).replace("\\", "\\\\")
        _ida_cmd_str  = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        _ps_script = (
            f'$p = Start-Process -FilePath {cmd[0]!r} '
            f'  -ArgumentList {",".join(repr(a) for a in cmd[1:])!r} '
            f'  -WorkingDirectory {str(IDA_DIR)!r} -PassThru; '
            f'Write-Host "IDA PID: $($p.Id)"; '
            f'Get-Content -Wait -Path {str(log.resolve())!r} '
            f'  -ErrorAction SilentlyContinue | Write-Host; '
            f'$p.WaitForExit()'
        )
        _ida_proc = subprocess.Popen(
            ["powershell", "-NoLogo", "-NoExit", "-Command", _ps_script],
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE
        )
    else:
        _ida_proc = subprocess.Popen(
            cmd,
            stdout=open(ida_out_log, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
            cwd=str(IDA_DIR),
        )

    def _kill_ida(*_):
        if _ida_proc and _ida_proc.poll() is None:
            _ida_proc.terminate()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT,  _kill_ida)
        signal.signal(signal.SIGTERM, _kill_ida)

    def _wait_for_ida(timeout_s, is_recovery=False):
        # Returns: "ok" | "license" | "crash_av" | "failed"
        for i in range(timeout_s):
            time.sleep(1)
            if _ida_port_open():
                return "ok"
            if _ida_proc.poll() is not None:
                rc = _ida_proc.returncode
                try:
                    log_text = log.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    log_text = ""
                _py_log_path = PROJECT_ROOT / "logs" / "ida_server_py.log"
                if not log_text.strip() and _py_log_path.exists():
                    try:
                        log_text = _py_log_path.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                tail = log_text.strip().splitlines()[-15:] if log_text.strip() else []
                _LICENSE_ERRORS = (
                    "no valid license", "license expired", "license not found",
                    "failed to get license", "evaluation period", "invalid license",
                    "unable to find license",
                )
                if any(p in log_text.lower() for p in _LICENSE_ERRORS):
                    print("  ⚠️   IDA license not activated — skipping")
                    return "license"
                # Self-heal: match crash code against signature table
                action = _IDA_CRASH_ACTIONS.get(rc)
                if action:
                    # Wipe the corrupt database every time IDA crashes so the
                    # next attempt (or next session) always gets a clean first-run.
                    for _ext in (".id0", ".id1", ".nam", ".til", ".idb"):
                        _dbp = IDA_PROJECTS / f"main.dol{_ext}"
                        if _dbp.exists():
                            try:
                                _dbp.unlink()
                            except Exception:
                                pass
                    if is_recovery:
                        # Already retried with recovery flags — IDA is unrecoverable this session
                        _report_incident("ida_crash_unrecoverable",
                                         {"rc": rc, "rc_hex": format(rc & 0xFFFFFFFF, "x")})
                        print()
                        print(f"  ╔══════════════════════════════════════════════════════╗")
                        print(f"  ║  IDA UNRECOVERABLE — STOPPING                        ║")
                        print(f"  ╠══════════════════════════════════════════════════════╣")
                        print(f"  ║  IDA crashed (rc=0x{rc & 0xFFFFFFFF:08X}) even with recovery flags.  ║")
                        print(f"  ║  This is a DOL-loader crash before ida_server.py     ║")
                        print(f"  ║  has a chance to apply safe-analysis settings.       ║")
                        print(f"  ║                                                      ║")
                        print(f"  ║  Try:                                                ║")
                        print(f"  ║    1. Delete data/ida_recovery.json and retry        ║")
                        print(f"  ║    2. Update/reinstall dol_loader.py in IDA/loaders/ ║")
                        print(f"  ║    3. Check logs/ida_server.log for loader errors    ║")
                        print(f"  ╚══════════════════════════════════════════════════════╝")
                        print()
                        sys.exit(1)
                    print(f"  ❗  IDA crashed (rc={rc}) — known crash: {action}")
                    print(f"  🔧  Writing recovery flag and retrying automatically ...")
                    _write_ida_recovery(action)
                    _report_incident("ida_crash", {"rc": rc, "rc_hex": format(rc & 0xFFFFFFFF, "x"), "file": "ida_server.py"})
                    return "crash_av"
                print(f"  ✖   IDA process died (rc={rc})")
                for ln in tail:
                    print(f"       {ln}")
                if not tail:
                    print(f"       (logs empty — check: idat.exe -A -c <dol>)")
                return "failed"
            if i % 30 == 29:
                print(f"  ⏳  Waiting for IDA ... ({i+1}s)")
        _report_incident("ida_timeout", {"timeout_s": timeout_s})
        print(f"  ✖   IDA didn't start in {timeout_s}s — aborting.")
        sys.exit(1)

    timeout = 360
    result  = _wait_for_ida(timeout)

    if result == "license":
        print()
        print("  ✖   IDA license not activated — cannot continue.")
        print("       Activate a valid IDA Pro license and retry.")
        sys.exit(1)

    if result == "crash_av":
        # Recovery flag written -- restart so ida_server.py reads it
        print("  🔄  Restarting IDA with recovery flags applied ...")
        if _ida_proc and _ida_proc.poll() is None:
            _ida_proc.terminate()
            try: _ida_proc.wait(timeout=10)
            except Exception: pass
        time.sleep(3)
        if sys.platform == "win32":
            _ps_script2 = (
                f'$p = Start-Process -FilePath {cmd[0]!r} '
                f'  -ArgumentList {",".join(repr(a) for a in cmd[1:])!r} '
                f'  -WorkingDirectory {str(IDA_DIR)!r} -PassThru; '
                f'Write-Host "IDA PID (recovery): $($p.Id)"; '
                f'Get-Content -Wait -Path {str(log.resolve())!r} '
                f'  -ErrorAction SilentlyContinue | Write-Host; '
                f'$p.WaitForExit()'
            )
            _ida_proc = subprocess.Popen(
                ["powershell", "-NoLogo", "-NoExit", "-Command", _ps_script2],
                creationflags=0x00000010,
            )
        else:
            _ida_proc = subprocess.Popen(
                cmd,
                stdout=open(ida_out_log, "w", encoding="utf-8", errors="replace"),
                stderr=subprocess.STDOUT,
                cwd=str(IDA_DIR),
            )
        result = _wait_for_ida(timeout, is_recovery=True)

    if result != "ok":
        print()
        print(f"  ✖   IDA failed to start (result={result!r}) — aborting.")
        sys.exit(1)


    # ida_server.py disables auto-analysis (INF_AF=0) to prevent the IDA 9
    # ACCESS_VIOLATION crash on the RUUE01 DOL.  Functions are created on demand
    # when /decompile is called.  Health check: just verify /ping responds.
    try:
        import httpx as _hx
        _r = _hx.get(f"http://localhost:{IDA_PORT}/ping", timeout=5.0)
        if _r.status_code == 200 and _r.json().get("ok"):
            print(f"  \u2705  IDA Hex-Rays ready on port {IDA_PORT} (on-demand mode)")
            return True
    except Exception:
        pass

    # /ping failed -- server never started.  Show logs for diagnosis.
    print("  \u26a0\ufe0f   IDA server did not respond to /ping.")
    _py_log = PROJECT_ROOT / "logs" / "ida_server_py.log"
    for _logfile in (_py_log, ida_out_log, PROJECT_ROOT / "logs" / "ida_server.log"):
        try:
            _txt = _logfile.read_text(encoding="utf-8", errors="replace").strip()
            if _txt:
                print(f"       --- {_logfile.name} (last 30 lines) ---")
                for _ln in _txt.splitlines()[-30:]:
                    print(f"         {_ln}")
                break
        except Exception:
            pass
    else:
        print("       (all log files empty)")
        print(f"       Try: idat.exe -A \"-S{IDA_SERVER}\" \"{IDA_PROJECTS / 'main.dol'}\"")
    return False


_IDA_HAS_CODE: bool = True   # informational only — IDA is never disabled by autopilot


def _validate_ida_code() -> bool:
    """
    Wait for IDA to populate functions after analysis.
    Polls /list_functions for up to 5 minutes, printing progress every 30s.
    IDA's targeted plan_and_wait on a full Wii DOL takes 1-3 minutes.
    Returns True when at least one function is found.
    """
    import httpx
    _POLL_INTERVAL = 10   # seconds between probes
    _TIMEOUT       = 300  # give IDA up to 5 minutes to analyse
    print(f"  ⏳  Waiting for IDA to analyse DOL (up to {_TIMEOUT//60} min) ...")
    last_report = 0
    for elapsed in range(0, _TIMEOUT, _POLL_INTERVAL):
        try:
            r = httpx.get(f"http://localhost:{IDA_PORT}/list_functions", timeout=10.0)
            if r.status_code == 200:
                funcs = r.json()
                if isinstance(funcs, list) and len(funcs) > 0:
                    print(f"  ✅  IDA analysis complete — {len(funcs):,} functions found "
                          f"(took ~{elapsed}s)")
                    return True
        except Exception:
            pass  # IDA still busy, keep waiting
        if elapsed - last_report >= 30:
            print(f"  ⏳  IDA still analysing ... {elapsed}s elapsed")
            last_report = elapsed
        time.sleep(_POLL_INTERVAL)

    # Timed out — IDA is running in on-demand mode (decompiles per-function
    # on each /decompile call even without pre-analysed function list)
    print(f"  ⚠️   IDA analysis did not complete in {_TIMEOUT}s.")
    print("       IDA will still decompile on-demand; Ghidra active as fallback.")
    return False


def run_ida_sync():
    """Sync IDA function names into symbol_hints.json via ghidra_sync.py --port 8081."""
    sync = PROJECT_ROOT / "tools" / "ghidra_sync.py"
    if not sync.exists() or not _ida_port_open():
        return
    print("  🔬  Syncing IDA symbols ...")
    r = subprocess.run(
        [sys.executable, str(sync), "--port", str(IDA_PORT)],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    for line in (r.stdout or "").splitlines():
        if any(k in line.lower() for k in ("hint", "connect", "skip", "saved", "+")):
            print(f"  {line.strip()}")
    print()


# ─── Apply objdiff symbol mappings ────────────────────────────────────────────

def run_apply_symbol_mappings():
    """
    If the user has mapped symbols in objdiff, push those renames to symbols.txt.
    """
    mapper = PROJECT_ROOT / "tools" / "apply_symbol_mappings.py"
    if not mapper.exists():
        return
    r = subprocess.run(
        [sys.executable, str(mapper)],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    for line in (r.stdout or "").splitlines():
        if line.strip() and "nothing" not in line.lower():
            print(f"  {line.strip()}")


# ─── Report ───────────────────────────────────────────────────────────────────

def regenerate_report():
    if OBJDIFF_CLI.exists():
        subprocess.run(
            [str(OBJDIFF_CLI), "report", "generate", "-o", str(REPORT_JSON)],
            cwd=PROJECT_ROOT, capture_output=True,
        )


def load_report() -> dict:
    if not REPORT_JSON.exists():
        return {}
    with open(REPORT_JSON) as f:
        return json.load(f)


# ─── Unit discovery ───────────────────────────────────────────────────────────

def discover_units(min_pct: float, max_pct: float) -> list[dict]:
    """
    Find all units from objdiff.json. For each, look up current match %
    from report.json. Returns list sorted highest-match-first.
    """
    if not OBJDIFF_JSON.exists():
        print("  ⚠  objdiff.json not found — run configure.py first")
        return []

    with open(OBJDIFF_JSON) as f:
        objdiff = json.load(f)

    report = load_report()
    report_by_name = {u["name"]: u for u in report.get("units", [])}

    units = []
    for unit in objdiff.get("units", []):
        name = unit.get("name", "")
        if not name or "_text" not in name:
            continue

        short = name.replace("main/", "")
        rep   = report_by_name.get(name, {})
        pct   = rep.get("measures", {}).get("fuzzy_match_percent", 0.0)

        if pct >= 100.0:
            continue  # already done
        if pct < min_pct or pct > max_pct:
            continue

        units.append({
            "name":  name,
            "short": short,
            "pct":   pct,
            "src":   unit.get("metadata", {}).get("source_path", ""),
        })

    units.sort(key=lambda u: -u["pct"])
    return units


# ─── History / model escalation ───────────────────────────────────────────────

def next_model_for(unit_name: str, model_ids: list[str]) -> str | None:
    if not HISTORY_FILE.exists():
        return None
    with open(HISTORY_FILE) as f:
        history = json.load(f)
    entry = history.get("units", {}).get(unit_name)
    if not entry or entry.get("matched"):
        return None
    next_level = entry.get("model_level", -1) + 1
    if next_level >= len(model_ids):
        return None
    return model_ids[next_level]


def is_fully_exhausted(unit_name: str, model_ids: list[str]) -> bool:
    if not HISTORY_FILE.exists():
        return False
    with open(HISTORY_FILE) as f:
        history = json.load(f)
    entry = history.get("units", {}).get(unit_name)
    if not entry or entry.get("matched"):
        return False
    return entry.get("model_level", -1) >= len(model_ids) - 1

def reset_exhausted_units(unit_names: list[str]) -> int:
    """
    Clear model_history entries for the given units so they can be retried
    from the first model.  Returns count reset.
    The healer may have added new symbol hints since the last attempt, so a
    fresh pass with the same models can produce a better result.
    """
    if not unit_names or not HISTORY_FILE.exists():
        return 0
    try:
        with open(HISTORY_FILE) as f:
            hist = json.load(f)
        units_hist = hist.get("units", {})
        reset = 0
        for name in unit_names:
            if name in units_hist and not units_hist[name].get("matched"):
                del units_hist[name]
                reset += 1
        hist["units"] = units_hist
        with open(HISTORY_FILE, "w") as f:
            json.dump(hist, f, indent=2)
        return reset
    except Exception as exc:
        print(f"  ⚠  reset_exhausted_units failed: {exc}")
        return 0


# ─── Parallel unit processing ─────────────────────────────────────────────────
#
# N_PARALLEL_WORKERS controls how many units run simultaneously.
# Windows dual-GPU: 2 workers — Worker A biased toward 7B (1060, port 11435),
#                   Worker B biased toward 14B (5060 Ti, port 11434).
#
N_PARALLEL_WORKERS = int(os.environ.get("DECOMP_WORKERS", "2"))

_PRINT_LOCK  = threading.Lock()
_ACTIVE_UNITS: dict[int, str] = {}
_ACTIVE_LOCK  = threading.Lock()


def _run_unit_worker(worker_id: int, unit: dict, model_ids: list[str],
                     max_attempts: int, no_commit: bool) -> tuple[bool, str]:
    """
    Run one unit in a worker thread.
    Bias worker 0 → qwen7b (GTX 1060) and worker 1 → qwen14b (RTX 5060 Ti)
    so both GPUs stay busy on separate units simultaneously.
    """
    unit_name  = unit["short"]
    next_model = next_model_for(unit_name, model_ids)

    if next_model is None and N_PARALLEL_WORKERS >= 2:
        next_model = model_ids[0] if worker_id % 2 == 0 else model_ids[min(1, len(model_ids) - 1)]

    cmd = [sys.executable, "-u", str(LOOP_SCRIPT), unit_name,
           "--max-attempts", str(max_attempts)]
    if next_model:
        cmd += ["--start-model", next_model]
    if no_commit:
        cmd.append("--no-commit")

    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{unit_name}.log"

    with _ACTIVE_LOCK:
        _ACTIVE_UNITS[worker_id] = unit_name
    with _PRINT_LOCK:
        print(f"\n  [W{worker_id}]  Starting: {unit_name}  ({unit['pct']:.1f}%)", flush=True)

    with open(log_file, "w", encoding="utf-8", errors="replace") as log_fh:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        lines = []
        try:
            for line in proc.stdout:
                log_fh.write(line)
                log_fh.flush()
                lines.append(line.rstrip())
        except Exception:
            pass
        finally:
            proc.wait()

    rc = proc.returncode
    matched = rc == 0
    # Extract match percentage from log output
    _match_pct = 0.0
    for _l in reversed(lines):
        import re as _re2
        _m = _re2.search(r"(\d+\.\d+)\s*%", _l)
        if _m:
            _match_pct = float(_m.group(1))
            break
    _record_match(unit_name, _match_pct)
    summary = [l for l in lines if any(
        k in l for k in ("🎉", "✅", "✗", "📊", "💰", "⚠", "Baseline:", "attempt", "%")
    )][-6:]

    with _PRINT_LOCK:
        print(f"\n  [W{worker_id}]  Done: {unit_name}  {'✅' if matched else '✗'}", flush=True)
        for l in summary:
            print(f"  [W{worker_id}]    {l.strip()}", flush=True)

    with _ACTIVE_LOCK:
        _ACTIVE_UNITS.pop(worker_id, None)

    if rc > 1:
        with _PRINT_LOCK:
            crash_msg = f"  [W{worker_id}]  ⚠  crashed (rc={rc}) — see logs/{unit_name}.log"
            # Check if crash was due to exhausted API credits
            _tail = "\n".join(lines[-20:]).lower()
            if "credit balance" in _tail or "credits exhausted" in _tail or "insufficient_quota" in _tail:
                crash_msg = (f"  [W{worker_id}]  !! CLAUDE API CREDITS EXHAUSTED "
                             f"— top up at console.anthropic.com or switch to Ollama-only")
                _report_incident("generic", {"error_type": "credit_balance"})
            print(crash_msg, flush=True)

    return matched, unit_name


def run_units_parallel(units: list[dict], model_ids: list[str],
                       max_attempts: int, no_commit: bool) -> tuple[list[str], list[str]]:
    """Process units with N_PARALLEL_WORKERS threads. Returns (succeeded, failed)."""
    succeeded, failed = [], []
    total = len(units)
    units_sorted = sorted(units, key=lambda u: -u["pct"])

    with ThreadPoolExecutor(max_workers=N_PARALLEL_WORKERS) as ex:
        futures = {
            ex.submit(_run_unit_worker, i % N_PARALLEL_WORKERS, u,
                      model_ids, max_attempts, no_commit): u
            for i, u in enumerate(units_sorted)
        }
        done = 0
        for fut in as_completed(futures):
            ok, name = fut.result()
            done += 1
            (succeeded if ok else failed).append(name)
            with _PRINT_LOCK:
                print(f"\n  Progress: {done}/{total} done  (✅ {len(succeeded)}  ✗ {len(failed)})",
                      flush=True)

    return succeeded, failed


# ─── Resource scraper (background) ────────────────────────────────────────────

def run_resource_scraper():
    """
    Launch resource_scraper.py in the background — downloads SDK headers and
    cross-game symbol files into data/scraped/. Cached; safe to call every run.
    """
    scraper = PROJECT_ROOT / "tools" / "resource_scraper.py"
    if not scraper.exists():
        return

    def _bg():
        try:
            r = subprocess.run(
                [sys.executable, str(scraper), "--quiet"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120,
            )
            if r.stdout and r.stdout.count("✔") > 0:
                print(f"\n  📦  Resource scraper: {r.stdout.count('✔')} file(s) updated",
                      flush=True)
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True, name="resource-scraper").start()



# ─── Run one unit ─────────────────────────────────────────────────────────────

def run_unit(unit: dict, model_ids: list[str], max_attempts: int,
             no_commit: bool) -> bool:
    unit_name  = unit["short"]
    next_model = next_model_for(unit_name, model_ids)

    cmd = [sys.executable, "-u", str(LOOP_SCRIPT), unit_name,
           "--max-attempts", str(max_attempts)]
    if next_model:
        print(f"  📚  Resuming from {next_model} (models below already tried)")
        cmd += ["--start-model", next_model]
    if no_commit:
        cmd.append("--no-commit")

    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{unit_name}.log"

    with open(log_file, "w") as log:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=0,
        )
        try:
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                sys.stdout.write(ch)
                sys.stdout.flush()
                log.write(ch)
        except KeyboardInterrupt:
            proc.terminate()
            raise
        finally:
            proc.wait()

    rc = proc.returncode
    if rc not in (0, 1):
        _report_incident("compile_error", {"rc": rc, "file": str(log_file)})
        # rc=2+ is typically an unhandled Python exception — surface the tail so
        # the user doesn't have to dig through logs to find the crash
        print(f"\n  ✖  decomp_loop crashed (rc={rc}) — last lines of {log_file.name}:")
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-15:]:
                print(f"     {line}")
        except Exception:
            pass
    return rc == 0


# ─── Free-Claude proxy auto-start ────────────────────────────────────────────

_PROXY_PROC: subprocess.Popen | None = None  # global handle so we can check it later


def _proxy_is_up(host: str = "127.0.0.1", port: int = 8082, timeout: float = 1.0) -> bool:
    """Return True if something is already listening on the proxy port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_free_proxy_running():
    """
    If ANTHROPIC_BASE_URL points at localhost:8082 and nothing is listening there yet,
    start the free-claude-code proxy as a background subprocess using uv.
    Logs a warning and continues if the proxy dir is missing or uv is not found.
    """
    global _PROXY_PROC

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if "8082" not in base_url:
        return  # proxy not requested

    if _proxy_is_up():
        print("  🔀  Free-Claude proxy already running on :8082")
        return

    proxy_dir = PROJECT_ROOT / "build" / "tools" / "free-claude-code"
    if not proxy_dir.exists():
        print("  ⚠  Free-Claude proxy dir not found — run: bash tools/setup_free_claude.sh")
        return

    uv_bin = "uv"
    try:
        subprocess.run([uv_bin, "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        # Try common uv install locations
        for candidate in [
            Path.home() / ".local" / "bin" / "uv",
            Path.home() / ".cargo" / "bin" / "uv",
        ]:
            if candidate.exists():
                uv_bin = str(candidate)
                break
        else:
            print("  ⚠  uv not found — can't auto-start proxy. Run: bash tools/setup_free_claude.sh")
            return

    print("  🔀  Starting free-claude-code proxy on :8082 …", flush=True)
    log_file = LOGS_DIR / "free_claude_proxy.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_file, "w")
    _PROXY_PROC = subprocess.Popen(
        [uv_bin, "run", "uvicorn", "server:app",
         "--host", "0.0.0.0", "--port", "8082",
         "--timeout-graceful-shutdown", "5"],
        cwd=proxy_dir,
        stdout=log_fh,
        stderr=log_fh,
        env={**os.environ},
    )

    # Wait up to 15s for it to come up
    for i in range(15):
        time.sleep(1)
        if _proxy_is_up():
            print(f"  ✅  Proxy up after {i+1}s  (log: logs/free_claude_proxy.log)")
            return

    print("  ⚠  Proxy did not come up in 15s — check logs/free_claude_proxy.log")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous ACCF decompilation — runs forever until done."
    )
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--min-pct", type=float, default=0.0,
                        help="Only work on units at or above this match %%")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="List units that would be processed and exit")
    args = parser.parse_args()

    model_ids = load_model_ids()
    pass_num  = 0

    while True:
        pass_num += 1
        print(f"\n{'='*62}")
        print(f"  🤖  Autopilot -- pass {pass_num}")
        print(f"{'='*62}\n")

        if pass_num == 1:
            ensure_free_proxy_running()  # start free-claude-code proxy if ANTHROPIC_BASE_URL set
            if ensure_ida_running():     # start IDA Pro headless (Hex-Rays decompiler, port 8081)
                _validate_ida_code()     # disable IDA for session if DOL loader didn't activate
            ensure_ghidra_running()      # start Ghidra headless (fallback decompiler, port 8080)
            run_resource_scraper()       # background-fetch SDK headers + cross-game symbols
            initial_push_and_ci_check()  # squash, push, wipe Actions, wait for CI

        delete_old_actions()
        refresh_build_ninja()
        run_apply_symbol_mappings()   # honour any manual objdiff renames first
        run_ida_sync()                # pull real names from IDA (no-op if offline)
        run_ghidra_sync()             # pull real names from Ghidra (no-op if offline)
        run_cross_game_symbols()      # NL/bbq cross-game class hints (fast, idempotent)
        run_symbol_hunter()           # scan ASM/src for inferred names

        # Check CI — if the current build is red, only wipe files that actually
        # fail to compile LOCALLY (prevents nuking good progress due to auth
        # failures or transient CI issues that don't reflect real compile errors)
        ci_status, ci_conclusion = get_ci_status()
        if ci_conclusion == "failure":
            print("  ⚠️   CI is failing — checking local compile to find real breakage …")
            broken = find_broken_sources()   # ninja dry-run locally
            if broken:
                print(f"  🔨  Locally broken: {broken} — wiping and re-decompiling\n")
                for unit_name in broken:
                    src_candidates = list(PROJECT_ROOT.glob(f"src/**/{unit_name}.c")) + \
                                     list(PROJECT_ROOT.glob(f"src/**/{unit_name}.cpp"))
                    for f in src_candidates:
                        f.write_text("#include <dolphin/types.h>\n")
                        print(f"  🗑   Wiped {f.relative_to(PROJECT_ROOT)}")
                    if HISTORY_FILE.exists():
                        with open(HISTORY_FILE) as hf:
                            hist = json.load(hf)
                        hist.get("units", {}).pop(unit_name, None)
                        with open(HISTORY_FILE, "w") as hf:
                            json.dump(hist, hf, indent=2)
                    run_unit({"short": unit_name, "pct": 0.0},
                             model_ids, args.max_attempts, args.no_commit)
            else:
                print("  ℹ️   CI failed but all files compile locally — "
                      "likely a transient/auth issue, not a code problem. Continuing.\n")
        elif ci_status:
            print(f"  ✅  CI: {ci_status}/{ci_conclusion or 'pending'}\n")

        regenerate_report()
        units = discover_units(min_pct=args.min_pct, max_pct=99.9)

        # Separate: has models left to try vs fully exhausted
        tryable   = [u for u in units if not is_fully_exhausted(u["short"], model_ids)]
        exhausted = [u for u in units if is_fully_exhausted(u["short"], model_ids)]

        if exhausted:
            print(f"  ⏭   {len(exhausted)} unit(s) fully exhausted (all models tried):")
            for u in exhausted:
                print(f"       {u['short']}  ({u['pct']:.1f}%)")
            print()

        if not tryable:
            if exhausted:
                print("  🏁  All remaining units exhausted all models. Nothing left to do.")
            else:
                print("  🎉  All units at 100%! Decomp complete.")
            break

        _total_units = len(units) + len(exhausted)
        _already_done = _total_units - len(units)
        print(f"  📋  {len(tryable)} unit(s) to process this pass  ({_already_done}/{_total_units} complete, {len(exhausted)} exhausted→retry):\n")
        for i, u in enumerate(tryable, 1):
            bar = "█" * int(u["pct"] / 5) + "░" * (20 - int(u["pct"] / 5))
            print(f"  [{i:>3}/{len(tryable)}]  {u['short']:<42}  {u['pct']:5.1f}%  [{bar}]")
        print()

        if args.dry_run:
            print("  Dry run — exiting.")
            break

        workers_label = (f"{N_PARALLEL_WORKERS} parallel workers"
                         if N_PARALLEL_WORKERS > 1 else "1 worker (sequential)")
        print(f"  🚀  Processing {len(tryable)} unit(s) with {workers_label} ...\n")

        # ── Service watchdog thread ───────────────────────────────────────────
        _watchdog_stop = threading.Event()

        def _watchdog():
            _credit_warned = False
            _ida_consec_failures = 0
            _IDA_MAX_CONSEC = 4   # give up restarting IDA after this many back-to-back crashes
            _ida_gave_up = False
            while not _watchdog_stop.is_set():
                _watchdog_stop.wait(timeout=120)
                if _watchdog_stop.is_set():
                    break
                if not _ghidra_port_open():
                    with _PRINT_LOCK:
                        print("  🔄  Watchdog: Ghidra went down — restarting ...", flush=True)
                    _report_incident("ghidra_down", {"source": "watchdog"})
                    ensure_ghidra_running()
                if not _ida_port_open():
                    if _ida_gave_up:
                        # Already hit the consecutive-crash ceiling — don't restart
                        pass
                    else:
                        _ida_consec_failures += 1
                        with _PRINT_LOCK:
                            print(f"  🔄  Watchdog: IDA went down — restarting "
                                  f"(attempt {_ida_consec_failures}/{_IDA_MAX_CONSEC}) ...",
                                  flush=True)
                        _report_incident("ida_port_stuck", {"source": "watchdog"})
                        ensure_ida_running()
                        # Give IDA 15 s to come up before deciding it crashed again
                        import time as _wtime
                        _wtime.sleep(15)
                        if _ida_port_open():
                            # Came back healthy — reset the counter
                            _ida_consec_failures = 0
                        elif _ida_consec_failures >= _IDA_MAX_CONSEC:
                            _report_incident("ida_gave_up",
                                             {"consec_failures": _ida_consec_failures})
                            with _PRINT_LOCK:
                                print(
                                    f"\n  ╔══════════════════════════════════════════════════════╗",
                                    flush=True)
                                print(
                                    f"  ║  WATCHDOG: IDA UNRECOVERABLE — STOPPING              ║",
                                    flush=True)
                                print(
                                    f"  ╠══════════════════════════════════════════════════════╣",
                                    flush=True)
                                print(
                                    f"  ║  IDA crashed {_IDA_MAX_CONSEC} times in a row during the run.     ║",
                                    flush=True)
                                print(
                                    f"  ║  Partial results saved — restart autopilot to retry. ║",
                                    flush=True)
                                print(
                                    f"  ╚══════════════════════════════════════════════════════╝\n",
                                    flush=True)
                            import os as _os
                            _os._exit(1)
                # Scan recent logs for credit exhaustion (do once per detection)
                if not _credit_warned:
                    try:
                        for _lf in LOGS_DIR.glob("*.log"):
                            _lt = _lf.read_text(encoding="utf-8", errors="replace")[-4000:]
                            if any(k in _lt.lower() for k in
                                   ("credit balance", "credits exhausted",
                                    "insufficient_quota", "your credit")):
                                with _PRINT_LOCK:
                                    print("  !! CLAUDE API CREDITS EXHAUSTED -- "
                                          "top up at console.anthropic.com "
                                          "or set ANTHROPIC_BASE_URL to local Ollama",
                                          flush=True)
                                _credit_warned = True
                                break
                    except Exception:
                        pass

        threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()

        try:
            if N_PARALLEL_WORKERS > 1:
                succeeded, failed = run_units_parallel(
                    tryable, model_ids, args.max_attempts, args.no_commit,
                )
            else:
                succeeded, failed = [], []
                for i, u in enumerate(tryable, 1):
                    print("\n──────────────────────────────────────────────────────────────")
                    print(f"  [{i}/{len(tryable)}]  {u['short']}  ({u['pct']:.1f}%)")
                    print("──────────────────────────────────────────────────────────────")
                    ok = run_unit(u, model_ids, args.max_attempts, args.no_commit)
                    (succeeded if ok else failed).append(u["short"])
                    delete_old_actions()
        finally:
            _watchdog_stop.set()

        delete_old_actions()

        print(f"\n{'='*62}")
        print(f"  Pass {pass_num} complete -- OK {len(succeeded)} matched, X {len(failed)} unmatched")
        print(f"{'='*62}")

        if not failed:
            print("\n  \xf0\x9f\x8e\x89  All units matched!")
            break

        # Check if any failed units still have a model to try
        still_tryable = [u for u in failed
                         if next_model_for(u, model_ids) is not None]
        if not still_tryable:
            if not failed:
                break  # everything matched -- done
            # All models exhausted -- reset history and retry from scratch.
            exhausted_names = [u if isinstance(u, str) else u["short"] if isinstance(u, dict) else str(u)
                               for u in failed]
            n_reset = reset_exhausted_units(exhausted_names)
            print(f"\n  \xe2\x99\xbb   All models exhausted on {len(failed)} unit(s). Resetting {n_reset} and cycling again ...")
            time.sleep(15)
            pass_num = 0
            continue

        print(f"\n  \xe2\x99\xbb   {len(still_tryable)} unit(s) will escalate to next model -- starting pass {pass_num + 1} ...")
        time.sleep(5)


if __name__ == "__main__":
    main()
