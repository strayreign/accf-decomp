"""
dol_loader.py — IDA Pro loader plugin for Nintendo GameCube / Wii DOL files.

Install: copy this file to <IDA_DIR>/loaders/dol_loader.py
Source:  GreenDog72/IDA-7-DOL-Loader (public domain)
Adapted for IDA 9: removed explicit set_processor_type (accept_file declares
the processor so IDA sets it before load_file runs); fixed ida_entry import.
"""

import os
import struct

import idaapi
import ida_entry
import ida_segment

DolFormatName = r'Nintendo GC\Wii DOL'

# Debug log — confirms whether IDA is calling accept_file at all.
_DEBUG_LOG = os.path.join(os.path.dirname(__file__), '..', 'logs', 'dol_loader_debug.log')


def _dlog(msg: str) -> None:
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
        with open(_DEBUG_LOG, 'a', encoding='utf-8') as _f:
            _f.write(msg + '\n')
    except Exception:
        pass


def read_int(li):
    return struct.unpack('>I', li.read(4))[0]


def accept_file(li, filename):
    """Called by IDA to check if this loader handles the file."""
    _dlog(f'accept_file: filename={filename!r}')

    if str(filename).lower().endswith('.dol'):
        _dlog('  -> accepted (.dol extension)')
        return {'format': DolFormatName, 'processor': 'ppc', 'priority': 100}

    # Content-based fallback: detect GC RAM load addresses in the DOL header.
    # DOL header bytes 0x48–0x90 hold the 7 text + 11 data load addresses.
    try:
        li.seek(0)
        hdr = li.read(0xe4)
        if len(hdr) >= 0xe4:
            text_addrs = [struct.unpack_from('>I', hdr, (18 + i) * 4)[0]
                          for i in range(7)]
            if any(0x80000000 <= a <= 0x81800000 for a in text_addrs if a != 0):
                _dlog('  -> accepted (GC RAM addresses detected in header)')
                return {'format': DolFormatName, 'processor': 'ppc', 'priority': 100}
    except Exception as _e:
        _dlog(f'  -> content check error: {_e}')

    _dlog('  -> rejected')
    return 0


def load_file(li, neflags, format):
    """Map DOL header segments into the IDA database."""
    _dlog('load_file called')
    li.seek(0)

    text_offset, text_addr, text_size = [], [], []
    data_offset, data_addr, data_size = [], [], []

    _dlog('[dol_loader] Parsing DOL header ...')

    for _ in range(7):
        text_offset.append(read_int(li))
    for _ in range(11):
        data_offset.append(read_int(li))
    for _ in range(7):
        text_addr.append(read_int(li))
    for _ in range(11):
        data_addr.append(read_int(li))
    for _ in range(7):
        text_size.append(read_int(li))
    for _ in range(11):
        data_size.append(read_int(li))

    bss_addr    = read_int(li)
    bss_size    = read_int(li)
    entry_point = read_int(li)

    _dlog('[dol_loader] Mapping segments ...')

    for i in range(7):
        if text_size[i] == 0:
            continue
        end = text_addr[i] + text_size[i]
        ida_segment.add_segm(0, text_addr[i], end, f'Text{i}', 'CODE')
        li.file2base(text_offset[i], text_addr[i], end, 1)
        _dlog(f'  Text{i}: 0x{text_addr[i]:08X}–0x{end:08X}')

    for i in range(11):
        if data_size[i] == 0:
            continue
        end = data_addr[i] + data_size[i]
        ida_segment.add_segm(0, data_addr[i], end, f'Data{i}', 'DATA')
        li.file2base(data_offset[i], data_addr[i], end, 1)
        _dlog(f'  Data{i}: 0x{data_addr[i]:08X}–0x{end:08X}')

    if bss_size:
        ida_segment.add_segm(0, bss_addr, bss_addr + bss_size, 'bss', 'BSS')
        _dlog(f'  bss:    0x{bss_addr:08X}–0x{bss_addr+bss_size:08X}')

    ida_entry.add_entry(entry_point, entry_point, 'entry', 1)

    _dlog(f'[dol_loader] Done. Entry=0x{entry_point:08X}  '
          f'BSS=0x{bss_addr:08X}+{bss_size:#x}')
    return 1
