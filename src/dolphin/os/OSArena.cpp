#include <dolphin/types.h>
#include <dolphin/os/OSCache.h>

// ---------------------------------------------------------------------------
// OS Arena / Memory Region Accessors
//
// The Wii has two memory regions: MEM1 (24 MB, 0x80000000) and MEM2
// (64 MB, 0x90000000, "BU1" / Hollywood eDRAM extension).  Each has
// independently maintained hi/lo boundaries (the "arena") managed by
// the OS.  Game code calls OSGetArenaHi/Lo to get the heap bounds and
// calls OSSetArenaHi/Lo to update them after allocating from the ends.
//
// All six getter/setters are thin wrappers around SDA-relative globals
// (small-data-area accessed via r13 in the original binary).
// ---------------------------------------------------------------------------

// SDA globals (r13-relative in the original binary)
// MEM1 arena:  [r13 - 0x2148] / [r13 - 0x3EC0] / [r13 - 0x3EBC]
// MEM2 arena:  separate slots
static void* sMEM1ArenaHi;   // [r13 - 0x2148]   (= r13 - 0x2148)
static void* sMEM2ArenaHi;   // [r13 - 0x2144]
static void* sMEM1ArenaLo;   // [r13 - 0x3EC0]
static void* sMEM2ArenaLo;   // [r13 - 0x3EBC]

// ---------------------------------------------------------------------------
// MEM1 arena
// ---------------------------------------------------------------------------

void* OSGetMEM1ArenaHi(void) {
    return sMEM1ArenaHi;
}

void* OSGetMEM1ArenaLo(void) {
    return sMEM1ArenaLo;
}

void OSSetMEM1ArenaHi(void* newHi) {
    sMEM1ArenaHi = newHi;
}

void OSSetMEM1ArenaLo(void* newLo) {
    sMEM1ArenaLo = newLo;
}

// ---------------------------------------------------------------------------
// MEM2 arena
// ---------------------------------------------------------------------------

void* OSGetMEM2ArenaHi(void) {
    return sMEM2ArenaHi;
}

void* OSGetMEM2ArenaLo(void) {
    return sMEM2ArenaLo;
}

void OSSetMEM2ArenaHi(void* newHi) {
    sMEM2ArenaHi = newHi;
}

void OSSetMEM2ArenaLo(void* newLo) {
    sMEM2ArenaLo = newLo;
}

// ---------------------------------------------------------------------------
// Generic accessors (return MEM1 values -- matches binary behaviour for
// standard configurations)
// ---------------------------------------------------------------------------

void* OSGetArenaHi(void) {
    return sMEM1ArenaHi;
}

void* OSGetArenaLo(void) {
    return sMEM1ArenaLo;
}
