# dtk-template Reference (for ACCF Decompilation)

Condensed from https://github.com/encounter/dtk-template/tree/main/docs

---

## 1. Project Setup (getting_started.md)

**Steps:** Create repo from template -> rename `orig/GAMEID` to game ID (RUUE01 for ACCF) -> place disc image or extracted files in `orig/RUUE01` -> configure `config/RUUE01/config.yml` -> generate `build.sha1` with `dtk shasum` -> run `python configure.py` -> run `ninja`.

**Map files:** If `.map` exists, add `map:` key to config.yml for initial analysis. Remove after symbols/splits are generated.

**ELF files:** `dtk elf config game.elf config/GAMEID` generates config, but splits need manual fixing.

**Post-analysis:** Must set up `__init_cpp_exceptions.cpp` split for C++ exception games. For GC 1.0-2.6 linkers (ACCF uses GC/1.3.2), need `.text`, `.ctors`, `.dtors`, `.sdata` splits for that file.

---

## 2. config.yml (config.example.yml)

Key settings for ACCF:

```yaml
name: main
object: sys/main.dol
object_base: orig/RUUE01
extract_objects: true
symbols: config/RUUE01/symbols.txt
splits: config/RUUE01/splits.txt
mw_comment_version: 10        # GC 1.3.2 = version 10
write_asm: true
detect_objects: true
detect_strings: true
fill_gaps: true
export_all: true
```

**VFS:** Paths support virtual filesystem — disc images (ISO/RVZ/WBFS) are read directly, Yaz0/Yay0 auto-decompressed.

**Modules (RELs):** Each REL gets its own `object`, `symbols`, `splits` entries under `modules:` list.

**Advanced:** `quick_analysis: true` skips function boundary analysis after initial run. `skip_cfa_ranges` for problematic code regions. `block_relocations` / `add_relocations` for fixups.

---

## 3. symbols.txt (symbols.md)

**Format:** `symbol_name = section:address; // [attributes]`

Example: `__dt__13mDoExt_bckAnmFv = .text:0x800DD2EC; // type:function size:0x5C scope:global align:4`

**Attributes (all optional):**

| Attribute | Values | Notes |
|-----------|--------|-------|
| `type:` | `function`, `object`, `label` | Symbol type |
| `size:` | hex/decimal | Symbol size in bytes |
| `scope:` | `global` (default), `local`, `weak` | Visibility |
| `align:` | number | Alignment |
| `data:` | `byte`, `2byte`, `4byte`, `8byte`, `float`, `double`, `string`, etc. | Data type for disassembly |
| `hidden` | (flag) | Hidden in generated object |
| `force_active` | (flag) | Prevents deadstripping, added to FORCEACTIVE |
| `noreloc` | (flag) | Contents not interpreted as addresses |
| `noexport` | (flag) | Excluded from export when `export_all` is on |
| `stripped` | (flag) | Was stripped by linker; affects common BSS |

**For C++:** Use mangled names (e.g., `__dt__13mDoExt_bckAnmFv`).

**Comments:** `//` or `#` lines allowed but NOT preserved when file is updated.

---

## 4. splits.txt (splits.md)

**Header** declares sections:
```yaml
Sections:
    .text       type:code align:32
    .ctors      type:rodata align:32
    .data       type:data align:32
    .bss        type:bss align:32
```

**File entries** map source files to address ranges:
```yaml
path/to/file.cpp:
    .text       start:0x80047E5C end:0x8004875C
    .data       start:0x803B1B40 end:0x803B1B60
    .bss        start:0x803DF828 end:0x803DFA8C
    .bss        start:0x8040D4AC end:0x8040D4D8 common
```

**File attributes:** `comment:` overrides mw_comment_version per file (use `comment:0` for non-mwcc files). `order:` influences link order for ambiguous cases.

**Section attributes:** `start:`, `end:`, `align:`, `rename:` (for `.ctors$10` etc.), `common` (BSS), `skip` (linker-generated data).

---

## 5. Common BSS (common_bss.md)

**What:** With `-common on`, mwcc generates global BSS symbols as common. Linker deduplicates them and places them at the END of `.bss`.

**In splits.txt:** Mark with `common` attribute on the `.bss` section entry.

**Detection:** If a `.bss` symbol near the end has XREFs from near the start of `.text`, likely common BSS. Also detectable via the inflation bug.

**Inflation bug (GC <=2.6 linkers — relevant for ACCF's GC/1.3.2):** The linker inflates the FIRST common symbol in a TU to the size of the entire TU's common section. Subtract subsequent symbol sizes to find the true size. Requires `.comment` section to reproduce.

---

## 6. .comment Section (comment_section.md)

**Purpose:** mwcc-generated section that mwld uses for symbol alignment and force-active/export flags. Without it, mwld won't adjust alignment or deadstrip.

**When needed:**
- Reproducing common BSS inflation bug (ACCF relevant)
- Newer linkers require it for common BSS
- Preventing linker from removing entire unused objects

**Binary format (at offset 0x0):**
- `0x00-0x0A`: Magic "CodeWarrior" (11 bytes)
- `0x0B`: Version — **10 (0x0A) for GC 1.3.2** (ACCF)
- `0x0C-0x0F`: Compiler version (major, minor, patch, 0x01)
- `0x10`: Pool data (0=disabled, 1=enabled)
- `0x11`: Float type (0=disabled, 1=software, 2=hardware)
- `0x12-0x13`: Processor type (0x0016 = Gekko)
- `0x14`: Unknown, always 0x2C
- `0x15`: Quirk flags bitfield
- `0x16-0x2B`: Padding
- `0x2C+`: Symbol entries (8 bytes each, one per ELF symbol)

**Symbol entry (8 bytes):**
- `0x0-0x3`: Alignment
- `0x4`: Visibility (0x00=default, 0x0D=weak)
- `0x5`: Active flags (0x00=default, 0x08=force_active/export)
- `0x6-0x7`: Padding

---

## 7. Dependencies (dependencies.md)

**Windows (our setup):** Python + ninja (native, no WSL needed). `pip install ninja`.

**macOS/Linux:** ninja + wibo (auto-downloaded Windows binary wrapper for mwcc).

---

## 8. GitHub Actions CI (github_actions.md)

Private `-build` repo stores game assets in a container image. Main repo references it in `build.yml`. Builds all versions on push/PR. Can publish to decomp.dev for progress tracking.

---

## ACCF-Specific Notes

- **Game ID:** RUUE01
- **Compiler:** mwcceppc GC/1.3.2 → `mw_comment_version: 10`
- **Common BSS inflation bug** applies (GC <=2.6 linker)
- **Platform:** Wii (but uses GC-era compiler)
- Our pipeline uses `objdiff-cli` for function-level diffs, which aligns with dtk-template's split-object approach
- `configure.py` is where per-file compiler flags are set (optimization level, inline depth, etc.)
