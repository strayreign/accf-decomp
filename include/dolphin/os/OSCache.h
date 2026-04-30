#ifndef DOLPHIN_OSCACHE_H
#define DOLPHIN_OSCACHE_H

#include <dolphin/types.h>

#ifdef __cplusplus
extern "C" {
#endif

void DCEnable(void);
void DCInvalidateRange(void* addr, u32 nBytes);
void DCFlushRange(void* addr, u32 nBytes);
void DCStoreRange(void* addr, u32 nBytes);
void DCFlushRangeNoSync(void* addr, u32 nBytes);
void DCStoreRangeNoSync(void* addr, u32 nBytes);
void DCZeroRange(void* addr, u32 nBytes);
void ICInvalidateRange(void* addr, u32 nBytes);
void ICFlashInvalidate(void);
void ICEnable(void);

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSCACHE_H
