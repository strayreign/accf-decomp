#ifndef DOLPHIN_OSCOND_H
#define DOLPHIN_OSCOND_H

#include <dolphin/os/OSMutex.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct OSCond {
    /* 0x00 */ OSThreadQueue queue;
} OSCond;

void OSInitCond(OSCond* cond);
void OSWaitCond(OSCond* cond, OSMutex* mutex);
void OSSignalCond(OSCond* cond);

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSCOND_H
