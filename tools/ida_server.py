"""
ida_server.py — Headless IDA Pro / Hex-Rays HTTP server for ACCF.

Run automatically by autopilot via ensure_ida_running().
Serves the same REST API as ghidra_server.py so decomp_loop.py uses
whichever decompiler is running (IDA preferred — better Hex-Rays output).

Manual invocation (from project root):
    idat.exe  -A -c -S"tools\\ida_server.py" ida_projects\\main.dol
    idat64.exe -A -c -S"tools\\ida_server.py" ida_projects\\main.dol

Endpoints (GhidraMCP-compatible):
    GET  /list_functions
    GET  /decompile?address=0x80001234
    GET  /get_function_by_name?name=FN
    POST /rename_function   body: {"old_name": "...", "new_name": "..."}
    GET  /ping

Port: 8081

Threading model
───────────────
IDA's Python API must only be called from IDA's main thread.
The HTTP server runs in a daemon thread.  Requests that need IDA API
access are placed in a work queue; a main-thread timer drains the queue
every 100 ms.  This is the only safe pattern for headless IDA scripts
that serve HTTP — execute_sync / t.join() fail because t.join() blocks
the main thread out of IDA's event loop, starving execute_sync.

DOL setup fallback
──────────────────
If the DOL loader plugin did not activate (0 functions after auto_wait),
this script manually parses the DOL header, sets the processor to PPC BE 32,
creates all code/data/BSS segments, fills them with data, and re-runs
auto-analysis.  This makes the server self-sufficient regardless of whether
dol_loader.py was picked up by IDA's loader discovery.
"""

import json
import os
import queue
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import ida_auto as _ida_auto   # for plan_and_wait
except ImportError:
    _ida_auto = None  # type: ignore

PORT = 8081

# ── File-based logging (idat.exe is a GUI app — stdout goes nowhere) ──────────

_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "ida_server_py.log"
_LOG_PATH.parent.mkdir(exist_ok=True)
_LOG_FILE = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)   # line-buffered


def _log(msg: str):
    _LOG_FILE.write(msg + "\n")
    _LOG_FILE.flush()


_log(f"[ida_server] Script started. Python {sys.version}")
_log(f"[ida_server] __file__ = {__file__}")

# ── IDA imports ───────────────────────────────────────────────────────────────
try:
    import idaapi
    import idautils
    import idc
    import ida_bytes
    import ida_entry
    import ida_funcs
    import ida_kernwin
    import ida_name
    import ida_segment
    import ida_hexrays

    _log("[ida_server] IDA imports OK")

    # ── Self-healing: apply recovery flags written by autopilot.py ───────────
    # If ensure_ida_running() detected a crash (e.g. ACCESS_VIOLATION during
    # analysis) it writes data/ida_recovery.json with the fix to apply.
    # We read that file here and self-apply the fix — no manual patching needed.
    _RECOVERY_FILE = Path(__file__).resolve().parent.parent / "data" / "ida_recovery.json"
    _recovery_flags: dict = {}
    if _RECOVERY_FILE.exists():
        try:
            import json as _rfj
            _recovery_flags = _rfj.loads(_RECOVERY_FILE.read_text(encoding="utf-8"))
            _log(f"[ida_server] Recovery flags: {_recovery_flags}")
        except Exception as _rfe:
            _log(f"[ida_server] WARNING: could not read recovery file: {_rfe}")
    
    if _recovery_flags.get("disable_auto_analysis"):
        # Previous run crashed during full auto-analysis (ACCESS_VIOLATION /
        # stack overflow).  Use *safe limited* analysis instead of full disable:
        #   - AF2 = 0  → no extra type inference / tail calls / far calls
        #   - AF strip FLIRT  → FLIRT signature matching is the most common
        #     PPC crash cause; remove it but keep CODE+PROC+USED for basic
        #     function discovery.
        try:
            idc.set_inf_attr(idc.INF_AF2, 0)
            _cur_af = idc.get_inf_attr(idc.INF_AF)
            # AF_FLIRT = 0x0020 in IDA 9; also clear AF_LVAR (0x0200) and
            # AF_TRFUNC (0x1000) which can cause deep recursion on PPC.
            _UNSAFE_BITS = 0x0020 | 0x0200 | 0x1000
            idc.set_inf_attr(idc.INF_AF, _cur_af & ~_UNSAFE_BITS)
            _log(
                f"[ida_server] SELF-HEAL: safe analysis mode "
                f"(AF=0x{idc.get_inf_attr(idc.INF_AF):04X} AF2=0)"
            )
        except Exception as _dae:
            # If we can't identify flags, fall all the way back to full disable
            try:
                idc.set_inf_attr(idc.INF_AF,  0)
                idc.set_inf_attr(idc.INF_AF2, 0)
            except Exception:
                pass
            _log(f"[ida_server] SELF-HEAL: full analysis disabled (fallback): {_dae}")
    # Extend with more recovery actions as new crash patterns are discovered:
    # if _recovery_flags.get("some_other_fix"):
    #     idc.set_something(...)


except ImportError as _e:
    _log(f"[ida_server] ERROR: Not running inside IDA — {_e}")
    sys.exit(1)


# ── DOL manual setup (fallback when loader plugin didn't activate) ────────────

def _setup_dol_manually() -> bool:
    """
    Parse the DOL file and build the IDA database from scratch.
    Called when auto_wait() completes with 0 functions.
    Returns True if at least one segment was created successfully.
    """
    dol_path = idc.get_input_file_path()
    _log(f"[ida_server] Manual DOL setup. Input file: {dol_path!r}")

    if not dol_path or not os.path.exists(dol_path):
        # Try to find it relative to the script
        candidate = Path(__file__).resolve().parent.parent / "ida_projects" / "main.dol"
        if candidate.exists():
            dol_path = str(candidate)
            _log(f"[ida_server] Using candidate DOL: {dol_path}")
        else:
            _log("[ida_server] ERROR: Cannot locate DOL file — manual setup aborted")
            return False

    try:
        with open(dol_path, "rb") as fh:
            dol_data = fh.read()
    except OSError as exc:
        _log(f"[ida_server] ERROR reading DOL: {exc}")
        return False

    _log(f"[ida_server] DOL size: {len(dol_data):#x} bytes")

    # Determine best byte-writing method available in this IDA version.
    # Priority:
    #   1. ida_bytes.put_bytes(ea, buf)    — IDA 9 preferred; initialises byte
    #      flags so analysis can see the bytes (same effect as file2base).
    #   2. ida_bytes.patch_bytes(ea, buf)  — fallback; in IDA 9 this does NOT
    #      initialise the backing flags so auto-analysis skips the range.
    #   3. patch_byte loop                 — last resort, byte-by-byte.
    _PUT_BYTES  = getattr(ida_bytes, "put_bytes",   None)
    _PATCH_BYTES = getattr(ida_bytes, "patch_bytes", None)
    if _PUT_BYTES is not None:
        _log("[ida_server] byte writer: ida_bytes.put_bytes (IDA 9 preferred)")
    elif _PATCH_BYTES is not None:
        _log("[ida_server] byte writer: ida_bytes.patch_bytes (fallback)")
    else:
        _log("[ida_server] byte writer: patch_byte loop (last resort)")

    def u32(off: int) -> int:
        return struct.unpack_from(">I", dol_data, off)[0]

    text_off  = [u32(i * 4)        for i in range(7)]
    data_off  = [u32((7  + i) * 4) for i in range(11)]
    text_addr = [u32((18 + i) * 4) for i in range(7)]
    data_addr = [u32((25 + i) * 4) for i in range(11)]
    text_size = [u32((36 + i) * 4) for i in range(7)]
    data_size = [u32((43 + i) * 4) for i in range(11)]
    bss_addr  = u32(54 * 4)
    bss_size  = u32(55 * 4)
    entry     = u32(56 * 4)

    _log(f"[ida_server] Entry=0x{entry:08X}  BSS=0x{bss_addr:08X}+{bss_size:#x}")

    # ── Set processor to PowerPC BE 32 (idempotent if -pppc was used) ──────────
    _log("[ida_server] Setting processor to ppc (PowerPC BE 32) ...")
    try:
        ok = idc.set_processor_type("ppc", 1)   # SETPROC_LOADER = 1
        _log(f"[ida_server] set_processor_type('ppc') -> {ok}")
    except Exception as exc:
        _log(f"[ida_server] WARNING: set_processor_type failed: {exc}")

    # ── Remove any existing segments (raw-binary placeholder segments) ────────
    segs = list(idautils.Segments())
    _log(f"[ida_server] Removing {len(segs)} existing segment(s) ...")
    for seg_ea in segs:
        # SEGMOD_KILL removes the segment AND its bytes so we get a clean slate
        ida_segment.del_segm(seg_ea, ida_segment.SEGMOD_KILL)

    # ── Add DOL segments ──────────────────────────────────────────────────────
    created = 0

    # is_loaded(ea) -> True if the byte is initialised in the IDA database.
    # Use this to verify whether a write method actually initialised the backing flags.
    _is_loaded = getattr(ida_bytes, "is_loaded", None)

    def _byte_is_init(ea: int) -> bool:
        if _is_loaded is not None:
            try:
                return bool(_is_loaded(ea))
            except Exception:
                pass
        # Fallback: idc.get_wide_byte returns 0xFF for void bytes BUT that
        # can also be a real byte value — so this is only a heuristic.
        return idc.get_wide_byte(ea) != 0xFF

    def _write_bytes(ea: int, data: bytes, name: str) -> bool:
        """Write bytes into the IDA database, using the best available method."""
        if not data:
            return True
        # 1. put_bytes — IDA 9 preferred (initialises byte flags properly)
        if _PUT_BYTES is not None:
            try:
                _PUT_BYTES(ea, data)
                if _byte_is_init(ea):
                    _log(f"[ida_server]   put_bytes OK for {name} @ 0x{ea:08X}")
                    return True
                _log(f"[ida_server]   put_bytes readback: byte NOT initialised @ 0x{ea:08X} "
                     f"(got 0x{idc.get_wide_byte(ea):02X}, expected 0x{data[0]:02X})")
            except Exception as _e:
                _log(f"[ida_server]   put_bytes failed for {name}: {_e}")
        # 2. patch_bytes fallback
        if _PATCH_BYTES is not None:
            try:
                _PATCH_BYTES(ea, data)
                init = _byte_is_init(ea)
                _log(f"[ida_server]   patch_bytes for {name} @ 0x{ea:08X}: "
                     f"is_loaded={init} "
                     f"readback=0x{idc.get_wide_byte(ea):02X} "
                     f"expected=0x{data[0]:02X}")
                if init:
                    return True
            except Exception as _e:
                _log(f"[ida_server]   patch_bytes failed for {name}: {_e}")
        # 3. Byte-by-byte patch_byte loop — slowest but most reliable
        _log(f"[ida_server]   patch_byte loop for {name} ({len(data)} bytes) ...")
        try:
            for _i, _b in enumerate(data):
                ida_bytes.patch_byte(ea + _i, _b)
            init = _byte_is_init(ea)
            _log(f"[ida_server]   patch_byte loop for {name}: is_loaded={init} "
                 f"readback=0x{idc.get_wide_byte(ea):02X} expected=0x{data[0]:02X}")
            return init
        except Exception as _e:
            _log(f"[ida_server]   patch_byte loop failed for {name}: {_e}")
            return False

    def _add_seg(start: int, size: int, file_offset: int,
                 name: str, seg_type: str) -> None:
        nonlocal created
        if size == 0 or start == 0:
            return
        end = start + size
        ret = ida_segment.add_segm(0, start, end, name, seg_type)
        if ret == 0:
            _log(f"[ida_server] WARNING: add_segm failed for {name} @ 0x{start:08X}")
            return
        if file_offset:
            data = dol_data[file_offset: file_offset + size]
            _write_bytes(start, data, name)
        created += 1
        _log(f"[ida_server] + {name:8s} 0x{start:08X}–0x{end:08X} ({size:#x})")

    for i in range(7):
        if text_size[i] and text_addr[i]:
            _add_seg(text_addr[i], text_size[i], text_off[i], f"Text{i}", "CODE")

    for i in range(11):
        if data_size[i] and data_addr[i]:
            _add_seg(data_addr[i], data_size[i], data_off[i], f"Data{i}", "DATA")

    if bss_size:
        _add_seg(bss_addr, bss_size, 0, "bss", "BSS")

    if created == 0:
        _log("[ida_server] ERROR: No segments created — DOL header may be invalid")
        return False

    # ── Entry point ───────────────────────────────────────────────────────────
    ida_entry.add_entry(entry, entry, "entry", 1)
    # Verify bytes at entry are accessible before attempting disassembly
    _eb = idc.get_wide_byte(entry)
    _einit = _byte_is_init(entry)
    _log(f"[ida_server] Byte at entry 0x{entry:08X} = 0x{_eb:02X} "
         f"is_loaded={_einit} "
         f"({'INITIALISED OK' if _einit else 'VOID — byte write FAILED'})")
    _isz = idc.create_insn(entry)
    _log(f"[ida_server] create_insn(0x{entry:08X}) -> {_isz} "
         f"({'OK' if _isz > 0 else 'FAILED — bytes may be uninitialised'})")
    if _isz > 0:
        _mnem = idc.print_insn_mnem(entry)
        _log(f"[ida_server] First insn at entry: {_mnem}")
    _afrc = idc.add_func(entry)
    _log(f"[ida_server] add_func(0x{entry:08X}) -> {_afrc}")
    _log(f"[ida_server] Entry point 0x{entry:08X} defined")

    # ── Queue background analysis on text segments ───────────────────────────
    # Do NOT call plan_and_wait / auto_wait here — they block the main thread
    # for 3-5 minutes on a full Wii DOL, preventing port 8081 from opening and
    # causing _wait_for_ida() to time out.
    #
    # Instead use auto_mark_range (non-blocking): IDA's internal background
    # analyser processes the queued ranges between _drain_queue timer ticks.
    # Functions appear progressively; /list_functions grows over time.
    _queued = 0
    for _i in range(7):
        if text_size[_i] and text_addr[_i]:
            _s, _e = text_addr[_i], text_addr[_i] + text_size[_i]
            try:
                idaapi.auto_mark_range(_s, _e, idaapi.AU_CODE)
                idaapi.auto_mark_range(_s, _e, idaapi.AU_FINAL)
                _queued += 1
                _log(f"[ida_server] Queued analysis: Text{_i} 0x{_s:08X}–0x{_e:08X}")
            except Exception as _qe:
                _log(f"[ida_server] auto_mark_range Text{_i}: {_qe}")
    _log(f"[ida_server] Manual segment setup complete — {created} segment(s), "
         f"{_queued} text segment(s) queued for background analysis.")
    return created > 0


# ── Startup: flush queue then check what the DOL loader gave us ──────────────
_log("[ida_server] Flushing initial analysis queue ...")
idaapi.auto_wait()

# Check whether the DOL loader activated (created at least one segment).
seg_count  = sum(1 for _ in idautils.Segments())
func_count = sum(1 for _ in idautils.Functions())
_log(f"[ida_server] Segments present: {seg_count}  Functions: {func_count}")


def _is_dol_database() -> bool:
    """
    Return True if at least one existing segment sits in the GameCube/Wii RAM
    range (0x80000000-0x81800000).  If all segments are at low/COFF addresses
    the DOL file was misparsed as a different format and we need manual setup.
    """
    for _ea in idautils.Segments():
        if 0x80000000 <= _ea <= 0x81800000:
            return True
    return False


_coff_misparse = (seg_count > 0) and not _is_dol_database()


def _save_database():
    """
    Save the IDA database to disk so segments survive across sessions.
    IDA 9 removed idc.SaveBase() — use idaapi.save_database() instead.
    Falls back through several APIs so it works across IDA versions.
    """
    # IDA 9+: idaapi.save_database(path="", flags=0)
    _sdb = getattr(idaapi, "save_database", None)
    if _sdb is not None:
        try:
            _sdb("", 0)
            _log("[ida_server] Database saved (idaapi.save_database).")
            return
        except Exception as _e:
            _log(f"[ida_server] idaapi.save_database failed: {_e}")
    # IDA 8 and earlier: idc.SaveBase("")
    _sb = getattr(idc, "SaveBase", None)
    if _sb is not None:
        try:
            _sb("")
            _log("[ida_server] Database saved (idc.SaveBase).")
            return
        except Exception as _e:
            _log(f"[ida_server] idc.SaveBase failed: {_e}")
    _log("[ida_server] WARNING: No save_database API found — segments will not persist.")


def _manual_setup_and_save():
    ok = _setup_dol_manually()
    if ok:
        # Save so correctly-mapped DOL segments survive the next session.
        # Without this, IDA sees "not closed" on next start and repairs back
        # to the COFF/empty state, causing the manual-setup loop every launch.
        _save_database()


if _coff_misparse:
    _log(
        f"[ida_server] COFF misparse detected — {seg_count} segment(s) exist "
        "but none are at GC/Wii RAM addresses. Forcing manual DOL setup ..."
    )
    _manual_setup_and_save()
elif seg_count == 0:
    _log("[ida_server] DOL loader did not activate — attempting manual DOL setup ...")
    _manual_setup_and_save()
elif func_count == 0:
    # DOL loader activated (segments present at correct GC RAM addresses) but
    # IDA found 0 functions — analysis hasn't run yet.
    #
    # FIX: do NOT call plan_and_wait / auto_wait here.  Those calls block the
    # main thread for 3–5 minutes on a full Wii DOL, preventing the HTTPServer
    # from binding port 8081.  _wait_for_ida() in autopilot.py would then time
    # out (360 s) and abort the whole session.
    #
    # Instead, queue the analysis ranges via auto_mark_range (non-blocking),
    # then start the HTTP server immediately.  IDA's internal event loop — kept
    # alive by the 100-ms _drain_queue timer — will process the queued work
    # concurrently with HTTP request serving.  Functions appear progressively;
    # /list_functions returns more results over time as analysis completes.
    _analysed = 0
    for _seg_ea in idautils.Segments():
        _seg = ida_segment.getseg(_seg_ea)
        if _seg is None:
            continue
        _seg_name = idc.get_segm_name(_seg_ea)
        # Only queue CODE / Text* segments — skip DATA/BSS
        if _seg.type not in (ida_segment.SEG_CODE,):
            if not (_seg_name or "").startswith("Text"):
                continue
        _start = _seg.start_ea
        _end   = _seg.size_ea   if hasattr(_seg, 'size_ea') else _seg.end_ea
        _end   = _seg.end_ea    # use end_ea; size_ea is not a real attribute
        _log(f"[ida_server] Queuing analysis: {_seg_name} 0x{_start:08X}–0x{_end:08X}")
        try:
            idaapi.auto_mark_range(_start, _end, idaapi.AU_CODE)
            idaapi.auto_mark_range(_start, _end, idaapi.AU_FINAL)
            _analysed += 1
        except Exception as _ae:
            _log(f"[ida_server] auto_mark_range failed for {_seg_name}: {_ae}")
    _log(
        f"[ida_server] DOL loader OK ({seg_count} segs) — {_analysed} text segment(s) "
        "queued for background analysis.  Functions will appear as IDA processes the queue."
    )
else:
    _log(
        f"[ida_server] DOL loader activated ({seg_count} segments, "
        f"{func_count} functions) — ready"
    )

# ── Hex-Rays ──────────────────────────────────────────────────────────────────
try:
    _HAS_HEXRAYS = ida_hexrays.init_hexrays_plugin()
    if _HAS_HEXRAYS:
        _log("[ida_server] Hex-Rays decompiler available.")
    else:
        _log("[ida_server] WARNING: Hex-Rays not available — /decompile will return empty.")
except Exception as _hr_exc:
    _log(f"[ida_server] Hex-Rays init error: {_hr_exc}")
    _HAS_HEXRAYS = False


# ── Work queue (main-thread dispatcher) ──────────────────────────────────────

_work_queue: queue.Queue = queue.Queue()


class _WorkItem:
    """A callable to run on the main thread with its result/error returned."""
    __slots__ = ("fn", "result", "error", "done")

    def __init__(self, fn):
        self.fn     = fn
        self.result = None
        self.error  = None
        self.done   = threading.Event()


def _run_on_main(fn):
    """
    Queue fn() for execution on IDA's main thread and block until done.
    Safe to call from any thread.  Returns fn()'s return value or raises.
    """
    item = _WorkItem(fn)
    _work_queue.put(item)
    item.done.wait()
    if item.error is not None:
        raise item.error
    return item.result


def _drain_queue():
    """
    Timer callback: drain the work queue on the main thread.
    Returns interval in ms for rescheduling (100 ms).
    """
    while True:
        try:
            item = _work_queue.get_nowait()
        except queue.Empty:
            break
        try:
            item.result = item.fn()
        except Exception as e:
            item.error = e
        item.done.set()
    return 100   # reschedule in 100 ms


# ── Helpers ───────────────────────────────────────────────────────────────────

def _addr_to_hex(ea: int) -> str:
    return f"{ea:08X}"


def _parse_addr(s: str):
    try:
        s = s.strip()
        if s.lower().startswith("0x"):
            return int(s, 16)
        if all(c in "0123456789abcdefABCDEF" for c in s):
            return int(s, 16)
        return int(s)
    except (ValueError, TypeError):
        return None


def _fn_dict(ea: int) -> dict:
    return {
        "name":    idc.get_func_name(ea) or f"sub_{ea:X}",
        "address": _addr_to_hex(ea),
    }


def _ensure_func_at(ea: int) -> None:
    """
    Ensure IDA has a function defined at ea, creating one on demand if needed.
    Uses targeted plan_and_wait on just this function's range — safe because
    global auto-analysis is disabled, so only this small range is analysed.
    """
    if ida_funcs.get_func(ea) is not None:
        return   # already known

    # Create the instruction stream first, then define the function
    idc.create_insn(ea)
    idc.add_func(ea)

    func = ida_funcs.get_func(ea)
    if func is None:
        _log(f"[ida_server] _ensure_func_at: add_func(0x{ea:X}) did not create function")
        return

    # Targeted analysis of just this function — safe, won't crash
    if _ida_auto is not None:
        try:
            _ida_auto.plan_and_wait(func.start_ea, func.end_ea)
            _log(f"[ida_server] plan_and_wait 0x{func.start_ea:X}–0x{func.end_ea:X} OK")
        except Exception as _pe:
            _log(f"[ida_server] plan_and_wait failed: {_pe}")


def _decompile_ea(ea: int) -> str:
    if not _HAS_HEXRAYS:
        return ""
    _ensure_func_at(ea)   # create function on demand if not yet analysed
    try:
        cfunc = ida_hexrays.decompile(ea)
        if cfunc:
            return str(cfunc)
    except Exception as e:
        _log(f"[ida_server] decompile 0x{ea:X}: {e}")
    return ""


def _find_func_by_name(name: str):
    ea = idc.get_name_ea_simple(name)
    if ea != idc.BADADDR:
        f = ida_funcs.get_func(ea)
        if f:
            return f.start_ea
    for ea in idautils.Functions():
        if idc.get_func_name(ea) == name:
            return ea
    return None


# ── Request handler ───────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request console noise

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse(self):
        p = urlparse(self.path)
        return p.path, parse_qs(p.query)

    def do_GET(self):
        path, qs = self._parse()

        if path in ("/ping", "/"):
            return self._json({"ok": True, "tool": "ida", "port": PORT})

        if path == "/list_functions":
            try:
                result = _run_on_main(
                    lambda: [_fn_dict(ea) for ea in idautils.Functions()]
                )
                return self._json(result)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path in ("/decompile", "/decompile_function"):
            raw = qs.get("address", qs.get("name", [""]))[0]
            try:
                ea = _parse_addr(raw)
                if ea is None:
                    ea = _run_on_main(lambda: _find_func_by_name(raw))
                if ea is None:
                    return self._json({"error": f"not found: {raw}"}, 404)
                _ea = ea
                code = _run_on_main(lambda: _decompile_ea(_ea))
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            if not code:
                return self._json({"error": "decompile failed"}, 500)
            return self._json({"decompiled": code})

        if path == "/get_function_by_name":
            name = qs.get("name", [""])[0]
            try:
                ea = _run_on_main(lambda: _find_func_by_name(name))
                if ea is None:
                    return self._json({"error": f"not found: {name}"}, 404)
                _ea = ea
                info = _run_on_main(lambda: {
                    "name":    idc.get_func_name(_ea),
                    "address": _addr_to_hex(_ea),
                    "size":    (ida_funcs.get_func(_ea).size()
                                if ida_funcs.get_func(_ea) else 0),
                })
                return self._json(info)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        self._json({"error": "unknown endpoint"}, 404)

    def do_POST(self):
        path, _ = self._parse()
        length  = int(self.headers.get("Content-Length", 0))
        body    = json.loads(self.rfile.read(length)) if length else {}

        if path == "/rename_function":
            old = body.get("old_name", "")
            new = body.get("new_name", "")
            try:
                ea = _run_on_main(lambda: _find_func_by_name(old))
                if ea is None:
                    return self._json({"error": f"not found: {old}"}, 404)
                _ea = ea
                ok = _run_on_main(
                    lambda: ida_name.set_name(_ea, new, ida_name.SN_FORCE)
                )
                if ok:
                    return self._json({"ok": True, "renamed": f"{old} -> {new}"})
                return self._json({"error": "rename failed"}, 500)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        self._json({"error": "unknown endpoint"}, 404)


# ── Entry point ─────────────────────────────────────────────────────────────

try:
    server = HTTPServer(("127.0.0.1", PORT), Handler)
except OSError as _bind_err:
    _log(f"[ida_server] FATAL: Cannot bind port {PORT}: {_bind_err}")
    _log("[ida_server] Is another idat.exe still running?  Kill it and retry.")
    raise

# Start HTTP server in a daemon thread — main thread must stay free for the
# work-queue timer so IDA API calls can be dispatched back to the main thread.
_server_thread = threading.Thread(target=server.serve_forever, daemon=True)
_server_thread.start()

final_count = sum(1 for _ in idautils.Functions())
_log(f"[ida_server] HTTP server started on port {PORT} -- {final_count} function(s) ready")

# Register a 100ms main-thread timer to drain the HTTP work queue.
# Keeps IDA event loop alive and lets background analyser process
# queued auto_mark_range work between timer ticks.
ida_kernwin.register_timer(100, _drain_queue)

# Script returns here. IDA stays alive because of the registered timer.
