#ifndef DOLPHIN_OSINTERRUPT_H
#define DOLPHIN_OSINTERRUPT_H

#include <dolphin/types.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef u32 OSInterruptMask;
typedef void (*OSInterruptHandler)(s16 exception, void* ctx);

BOOL OSDisableInterrupts(void);
BOOL OSEnableInterrupts(void);
BOOL OSRestoreInterrupts(BOOL level);

void __OSMaskInterrupts(OSInterruptMask mask);
void __OSUnmaskInterrupts(OSInterruptMask mask);
OSInterruptHandler OSSetExceptionHandler(s16 exception, OSInterruptHandler handler);

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSINTERRUPT_H
