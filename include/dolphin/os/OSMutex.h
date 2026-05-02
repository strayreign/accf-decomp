#ifndef DOLPHIN_OSMUTEX_H
#define DOLPHIN_OSMUTEX_H

#include <dolphin/os/OSThread.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct OSMutex {
    /* 0x00 */ OSThreadQueue queue;
    /* 0x08 */ OSThread* thread;
    /* 0x0C */ int count;
    /* 0x10 */ OSMutex* next;
    /* 0x14 */ OSMutex* prev;
} OSMutex;

void OSInitMutex(OSMutex* mutex);
void OSLockMutex(OSMutex* mutex);
void OSUnlockMutex(OSMutex* mutex);

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSMUTEX_H
