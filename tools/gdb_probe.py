#!/usr/bin/env python3
"""
gdb_probe.py — Runtime function probe via Dolphin's GDB stub.

Connects to a running Dolphin instance (GDB stub on port 2345), sets a
breakpoint at a function address, and when the game hits it captures:
  • PPC register values (r3–r10, f1–f8, lr, sp)
  • Memory dump of the object pointed to by r3 (the 'this' pointer in C++)
  • Memory dump of any additional pointer args (r4–r6 if they look like Wii RAM)

This runtime context is fed into decomp_loop.py's LLM prompt so the model
can recover struct field offsets from live data rather than guessing.

Mac:   GDB via Homebrew/Xcode  — brew install gdb
       (also requires code-signing; see README)
Win:   GDB via MSYS2           — pacman -S mingw-w64-x86_64-gdb
       OllyDbg does NOT support PowerPC — use GDB instead.

Dolphin setup:
  1. Options → Configuration → GameCube → Enable GDB Stub: ✓
     (or launch: dolphin-emu --debugger, then Emulation → GDB)
  2. GDB stub port: 2345  (default)
  3. Start the game, let it reach the menu (functions need to be loaded).

Usage:
  python3 tools/gdb_probe.py 802C5394          # probe function at that address
  python3 tools/gdb_probe.py 802C5394 --once   # capture one hit then exit
  python3 tools/gdb_probe.py 802C5394 --hits 3 # capture 3 hits

Autopilot integration:
  If Dolphin's stub is reachable, decomp_loop.py will call probe_function()
  and prepend the result to the LLM prompt as "=== Runtime trace ===".
  Set DOLPHIN_GDB_PORT=0 to disable even if Dolphin is running.
"""

import argparse
import os
import re
import socket
import struct
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── RSP (GDB Remote Serial Protocol) client ───────────────────────────────────

STUB_HOST = os.environ.get("DOLPHIN_GDB_HOST", "127.0.0.1")
STUB_PORT = int(os.environ.get("DOLPHIN_GDB_PORT", "2345"))

# Wii RAM range: 0x80000000–0x817FFFFF (main MEM1)
_WII_RAM_LO = 0x80000000
_WII_RAM_HI = 0x817FFFFF

# Number of bytes to dump from pointer arguments
_PTR_DUMP_BYTES = 128


class RSPClient:
    """Minimal GDB Remote Serial Protocol client for PowerPC targets."""

    def __init__(self, host: str = STUB_HOST, port: int = STUB_PORT, timeout: float = 5.0):
        self._s = socket.socket()
        self._s.settimeout(timeout)
        self._s.connect((host, port))
        self._buf = b""
        # Ack the initial '+' from stub
        try:
            self._s.recv(1)
        except Exception:
            pass
        # Disable ACK mode (GDB >= 7 feature, harmless if unsupported)
        self._send_raw(b"+")

    def _checksum(self, data: bytes) -> bytes:
        cs = sum(data) & 0xFF
        return f"{cs:02x}".encode()

    def _send_raw(self, pkt: bytes):
        self._s.sendall(pkt)

    def _send(self, cmd: bytes) -> str:
        pkt = b"$" + cmd + b"#" + self._checksum(cmd)
        self._s.sendall(pkt)
        return self._recv()

    def _recv(self) -> str:
        while True:
            chunk = self._s.recv(4096)
            if not chunk:
                raise ConnectionError("Stub disconnected")
            self._buf += chunk
            # Look for complete $..#xx packet
            if b"#" in self._buf:
                start = self._buf.find(b"$")
                if start == -1:
                    self._buf = b""
                    continue
                end = self._buf.find(b"#", start)
                if end == -1 or len(self._buf) < end + 3:
                    continue  # need 2 more checksum bytes
                payload = self._buf[start + 1:end].decode(errors="replace")
                self._buf = self._buf[end + 3:]
                self._send_raw(b"+")  # ACK
                return payload

    def halt(self):
        """Send Ctrl-C to halt execution."""
        self._send_raw(b"\x03")
        try:
            self._recv()
        except Exception:
            pass

    def cont(self):
        """Continue execution."""
        pkt = b"c"
        p = b"$" + pkt + b"#" + self._checksum(pkt)
        self._s.sendall(p)

    def wait_for_stop(self, timeout: float = 30.0) -> str:
        """Block until the stub sends a stop reply (breakpoint hit, etc.)."""
        old_timeout = self._s.gettimeout()
        self._s.settimeout(timeout)
        try:
            return self._recv()
        finally:
            self._s.settimeout(old_timeout)

    def set_breakpoint(self, addr: int):
        """Set a software breakpoint at addr."""
        self._send(f"Z0,{addr:x},4".encode())

    def clear_breakpoint(self, addr: int):
        """Remove a software breakpoint at addr."""
        self._send(f"z0,{addr:x},4".encode())

    def read_registers(self) -> dict:
        """
        Read all PPC general + float registers.
        Returns dict: {"r0"..r31, "f0"..f31, "pc", "lr", "cr", "xer", "ctr", "sp"}.
        PowerPC register file in RSP order (per GDB's ppc-eabi.xml):
          0–31: GPRs, 32–63: FPRs (8-byte each), 64: pc, 65: msr,
          66: cr, 67: lr, 68: ctr, 69: xer
        """
        raw = self._send(b"g")
        regs = {}

        # GPRs (0–31): 4 bytes each = 8 hex chars
        for i in range(32):
            hi = i * 8
            val = int(raw[hi:hi + 8], 16)
            regs[f"r{i}"] = val
        regs["sp"] = regs["r1"]

        # FPRs (32–63): 8 bytes each = 16 hex chars
        fpr_base = 32 * 8
        for i in range(32):
            hi = fpr_base + i * 16
            raw_f = bytes.fromhex(raw[hi:hi + 16])
            val = struct.unpack(">d", raw_f)[0]
            regs[f"f{i}"] = val

        # Special registers
        spec_base = fpr_base + 32 * 16
        # pc, msr, cr, lr, ctr, xer
        names = ["pc", "msr", "cr", "lr", "ctr", "xer"]
        for j, name in enumerate(names):
            hi = spec_base + j * 8
            regs[name] = int(raw[hi:hi + 8], 16)

        return regs

    def read_memory(self, addr: int, length: int) -> bytes | None:
        """Read `length` bytes from Wii RAM. Returns None on error."""
        resp = self._send(f"m{addr:x},{length:x}".encode())
        if resp.startswith("E") or len(resp) < length * 2:
            return None
        try:
            return bytes.fromhex(resp[:length * 2])
        except ValueError:
            return None

    def close(self):
        try:
            self.cont()  # leave the game running
        except Exception:
            pass
        self._s.close()


# ── Probe logic ───────────────────────────────────────────────────────────────

def _is_wii_ptr(val: int) -> bool:
    return _WII_RAM_LO <= val <= _WII_RAM_HI


def _hex_dump(data: bytes, base_addr: int, cols: int = 16) -> str:
    lines = []
    for i in range(0, len(data), cols):
        chunk = data[i:i + cols]
        hex_part  = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {base_addr + i:08X}:  {hex_part:<{cols*3}}  {ascii_part}")
    return "\n".join(lines)


def probe_once(client: RSPClient, addr: int, hit_num: int = 1) -> str:
    """
    Wait for the next breakpoint hit at addr and return a formatted report.
    Caller is responsible for setting the breakpoint before calling this.
    """
    regs = client.read_registers()

    lines = [f"=== Runtime trace (hit #{hit_num} @ 0x{addr:08X}) ==="]

    # Argument registers
    lines.append("\nArgument / scratch registers:")
    for i in range(3, 11):  # r3–r10
        val = regs[f"r{i}"]
        ptr_note = "  ← Wii ptr" if _is_wii_ptr(val) else ""
        lines.append(f"  r{i:2d} = 0x{val:08X}{ptr_note}")

    # Float args
    float_args = [(f"f{i}", regs[f"f{i}"]) for i in range(1, 9)
                  if regs[f"f{i}"] != 0.0]
    if float_args:
        lines.append("\nFloat argument registers:")
        for name, val in float_args:
            lines.append(f"  {name:3s} = {val!r}")

    # Special regs
    lines.append(f"\n  lr  = 0x{regs['lr']:08X}  (return address → caller)")
    lines.append(f"  sp  = 0x{regs['sp']:08X}")
    lines.append(f"  pc  = 0x{regs['pc']:08X}")

    # Memory dumps for pointer args
    dumped = []
    for i in range(3, 7):  # r3–r6
        val = regs[f"r{i}"]
        if _is_wii_ptr(val):
            data = client.read_memory(val, _PTR_DUMP_BYTES)
            if data:
                dumped.append((f"r{i}", val, data))

    if dumped:
        lines.append("\nMemory at pointer arguments:")
        for reg_name, ptr, data in dumped:
            lines.append(f"\n  [{reg_name} = 0x{ptr:08X}] ({_PTR_DUMP_BYTES} bytes):")
            lines.append(_hex_dump(data, ptr))

    return "\n".join(lines)


def probe_function(addr: int, hits: int = 1,
                   host: str = STUB_HOST, port: int = STUB_PORT,
                   hit_timeout: float = 60.0) -> str | None:
    """
    High-level API used by decomp_loop.py.
    Returns a formatted runtime trace string, or None if the stub is
    unreachable / disabled (DOLPHIN_GDB_PORT=0).

    addr      — function entry address (int)
    hits      — how many breakpoint hits to capture before returning
    host/port — Dolphin GDB stub address
    hit_timeout — seconds to wait for each hit
    """
    if port == 0:
        return None

    try:
        client = RSPClient(host, port, timeout=3.0)
    except OSError:
        return None  # Dolphin not running or stub not enabled — silently skip

    try:
        client.halt()
        client.set_breakpoint(addr)
        client.cont()
        print(f"  🔴  GDB breakpoint set @ 0x{addr:08X} — play game until function is called …",
              flush=True)

        reports = []
        for hit_num in range(1, hits + 1):
            stop = client.wait_for_stop(timeout=hit_timeout)
            if not stop.startswith("S") and not stop.startswith("T"):
                print(f"  ⚠  GDB: unexpected stop reply: {stop!r}")
                break
            reports.append(probe_once(client, addr, hit_num))
            if hit_num < hits:
                client.cont()

        client.clear_breakpoint(addr)
        return "\n\n".join(reports) if reports else None

    except Exception as e:
        print(f"  ⚠  GDB probe error: {e}")
        return None
    finally:
        client.close()


def dolphin_stub_reachable(host: str = STUB_HOST, port: int = STUB_PORT) -> bool:
    """Quick check — is Dolphin's GDB stub up? Used by decomp_loop.py."""
    if port == 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Probe a function in a running Dolphin instance via GDB stub."
    )
    parser.add_argument("address", help="Function address (hex, e.g. 802C5394)")
    parser.add_argument("--hits",  type=int, default=1,
                        help="Number of breakpoint hits to capture (default 1)")
    parser.add_argument("--host",  default=STUB_HOST)
    parser.add_argument("--port",  type=int, default=STUB_PORT)
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Seconds to wait for each hit (default 60)")
    args = parser.parse_args()

    addr_str = args.address.lstrip("0x").upper()
    try:
        addr = int(addr_str, 16)
    except ValueError:
        print(f"Bad address: {args.address!r}", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to Dolphin GDB stub at {args.host}:{args.port} …")
    result = probe_function(addr, hits=args.hits,
                            host=args.host, port=args.port,
                            hit_timeout=args.timeout)
    if result is None:
        print("Could not connect. Is Dolphin running with GDB stub enabled?")
        print("")
        print("Dolphin setup:")
        print("  Options → Configuration → GameCube → Enable GDB Stub ✓")
        print("  Port: 2345")
        sys.exit(1)

    print(result)


if __name__ == "__main__":
    main()
