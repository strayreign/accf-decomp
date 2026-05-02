#ifndef DOLPHIN_OS_IPC_H
#define DOLPHIN_OS_IPC_H

#include <dolphin/types.h>

#ifdef __cplusplus
extern "C" {
#endif

// Hardware IPC register base
#define IPC_BASE_ADDR 0xCD000000

// IPC hardware register indices (each 4 bytes wide)
#define IPC_REG_PPCMSG     0   // PPC->ARM message register  (0xCD000000)
#define IPC_REG_PPCCTRL    1   // PPC IPC control register   (0xCD000004)
#define IPC_REG_ARMMSG     2   // ARM->PPC message register  (0xCD000008)
#define IPC_REG_ARMCTRL    3   // ARM IPC control register   (0xCD00000C)

// IOS error codes
#define IOS_OK              0
#define IOS_EACCES         -1
#define IOS_EEXIST         -2
#define IOS_EINVAL         -4
#define IOS_EMAX           -5
#define IOS_ENOENT         -6
#define IOS_EQUEUEFULL     -8
#define IOS_ENOMEM         -22

// Low-level buffer accessors (dolphin/os/IPC.c -- Matching)
u32 IPCGetBufferHi(void);
u32 IPCGetBufferLo(void);
void IPCSetBufferLo(u32 lo);

// Hardware register accessors
u32 IPCReadReg(int reg);
void IPCWriteReg(int reg, u32 val);

// Initialization
void IPCInit(void);
s32 IPCCltInit(void);

// strnlen used internally by IOS path copy
int strnlen(const char* s, int n);

// IOS async callback type
typedef void (*IOSAsyncCallback)(s32 result, void* usrData);

// IOS high-level API
s32 IOS_Open(const char* path, u32 mode);
void IOS_OpenAsync(const char* path, u32 mode, IOSAsyncCallback callback, void* usrData);
void IOS_IoctlAsync(s32 fd, u32 ioctl, void* inBuf, u32 inLen, void* ioBuf, u32 ioLen,
                    IOSAsyncCallback callback, void* usrData);
void IOS_Ioctlv(s32 fd, u32 ioctl, u32 cnt, u32 cntOut, void* vec);

// IPC heap
s32  iosCreateHeap(void* addr, s32 size);
void* iosAllocAligned(s32 heap, u32 size, u32 align);
s32  iosFree(s32 heap, void* ptr);

// Profiling
void IPCiProfInit(void);
void IPCiProfQueueReq(void* req, u32 cmd);

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OS_IPC_H
