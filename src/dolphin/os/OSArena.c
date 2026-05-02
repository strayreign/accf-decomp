#include <dolphin/types.h>
#include <dolphin/os/OSArena.h>

#define ROUND_UP(x, align) (((u32)(x) + (u32)(align) - 1) & ~((u32)(align) - 1))

static void* __OSArenaHi  : 0x8074F7F8;
static void* s_mem2ArenaHi : 0x8074F7FC;
static void* __OSArenaLo  : 0x8074DA80;
static void* s_mem2ArenaLo : 0x8074DA84;

void* OSGetMEM1ArenaHi(void) { return __OSArenaHi; }
void* OSGetMEM2ArenaHi(void) { return s_mem2ArenaHi; }
void* OSGetArenaHi(void)     { return __OSArenaHi; }
void* OSGetMEM1ArenaLo(void) { return __OSArenaLo; }
void* OSGetMEM2ArenaLo(void) { return s_mem2ArenaLo; }
void* OSGetArenaLo(void)     { return __OSArenaLo; }

void OSSetMEM1ArenaHi(void* hi) { __OSArenaHi   = hi; }
void OSSetMEM2ArenaHi(void* hi) { s_mem2ArenaHi = hi; }
void OSSetArenaHi(void* hi)     { __OSArenaHi   = hi; }
void OSSetMEM1ArenaLo(void* lo) { __OSArenaLo   = lo; }
void OSSetMEM2ArenaLo(void* lo) { s_mem2ArenaLo = lo; }
void OSSetArenaLo(void* lo)     { __OSArenaLo   = lo; }

void* OSAllocFromMEM1ArenaLo(u32 size, u32 align) {
    void* lo = __OSArenaLo;
    lo = (void*)ROUND_UP(lo, align);
    __OSArenaLo = (void*)ROUND_UP((u32)lo + size, align);
    return lo;
}
