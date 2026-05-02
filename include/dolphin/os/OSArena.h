#ifndef DOLPHIN_OSARENA_H
#define DOLPHIN_OSARENA_H

#ifdef __cplusplus
extern "C" {
#endif

void* OSGetMEM1ArenaHi(void);
void* OSGetMEM2ArenaHi(void);
void* OSGetArenaHi(void);
void* OSGetMEM1ArenaLo(void);
void* OSGetMEM2ArenaLo(void);
void* OSGetArenaLo(void);

void OSSetMEM1ArenaHi(void* hi);
void OSSetMEM2ArenaHi(void* hi);
void OSSetArenaHi(void* hi);
void OSSetMEM1ArenaLo(void* lo);
void OSSetMEM2ArenaLo(void* lo);
void OSSetArenaLo(void* lo);

void* OSAllocFromMEM1ArenaLo(u32 size, u32 align);

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSARENA_H
