#include <dolphin/os/IPC.h>
#include <dolphin/os/OSThread.h>
#include <dolphin/os/OSInterrupt.h>

// ---------------------------------------------------------------------------
// Hardware register layout at 0xCD000000
// (IPC_BASE_ADDR defined in IPC.h)
// ---------------------------------------------------------------------------

// IPC queue depth (power-of-two, must match ARM-side expectation)
#define IPC_QUEUE_SIZE  16
#define IPC_QUEUE_MASK  (IPC_QUEUE_SIZE - 1)

// Magic cookie written by iosCreateHeap to identify free blocks
#define IOS_HEAP_MAGIC_FREE  0xBabe0000
#define IOS_HEAP_MAGIC_ALLOC 0xBabe0001
#define IOS_HEAP_ALIGN_PAD   0xBabe0002

// IPC IOS request command codes
#define IOS_CMD_OPEN       1
#define IOS_CMD_IOCTLV     7

// ---------------------------------------------------------------------------
// IPC control register flag bits (IPC_REG_PPCCTRL)
// ---------------------------------------------------------------------------
#define IPC_CTRL_X1        0x01   // Execute: send message to ARM
#define IPC_CTRL_Y2        0x02   // ACK of ARM->PPC message
#define IPC_CTRL_Y1        0x04   // ACK of PPC->ARM message (Y1 set when ARM received)
#define IPC_CTRL_X2        0x08   // ARM->PPC message ready
#define IPC_CTRL_IY1       0x10   // Interrupt enable for Y1
#define IPC_CTRL_IY2       0x20   // Interrupt enable for Y2
// Full "ready to send" flag set
#define IPC_CTRL_SEND      (IPC_CTRL_IY1 | IPC_CTRL_IY2 | IPC_CTRL_X1)

// ---------------------------------------------------------------------------
// IPC global state (SDA-relative in the original binary)
// ---------------------------------------------------------------------------

// Ring buffer for outgoing IOS requests
static void*   sIPCRequestQueue[IPC_QUEUE_SIZE];   // [8065EFF0]
static u32     sIPCQueueHead;   // consumer index (ARM has read up to here)  [8065EFE0]
static u32     sIPCQueueTail;   // producer index (PPC has written up to here) [8065EFE4]
static u32     sIPCQueueIn;    // next write slot  [8065EFEC]
static u32     sIPCQueueOut;   // next read slot   [8065EFE8]

// Number of outstanding hardware send credits (decrement when we kick ARM,
// increment when ARM ACKs).  Starts at 1 after IPCInit.
static s32     sIPCSendCount;   // [r13 - 0x4D00]

// Non-NULL while a synchronous call is sleeping
static void*   sIPCSyncRequest;          // [r13 - 0x2960]
static OSThreadQueue* sIPCSyncQueue;     // [r13 - 0x295C]

// Cached copy of IPC buffer hi/lo pointers (set by IPCInit)
static u32     sIPCBufHi;       // [r13 - 0x2968]
static u32     sIPCBufLo;       // [r13 - 0x296C / -0x2974]

// Init flags
static BOOL    sIPCInitialized;    // [r13 - 0x2978]
static BOOL    sIPCCltInitialized; // [r13 - 0x2954]

// IPC heap handle (opaque index returned by iosCreateHeap)
static s32     sIPCHeap;        // [r13 - 0x4CFC]

// ---------------------------------------------------------------------------
// IPCInit
//   Initialises the IPC buffer pointers from the OS IPC buffer region.
//   Safe to call multiple times (guarded by sIPCInitialized).
// ---------------------------------------------------------------------------
void IPCInit(void) {
    if (!sIPCInitialized) {
        sIPCBufHi = IPCGetBufferHi();
        sIPCBufLo = IPCGetBufferLo();
        // working copies used by the client layer
        sIPCBufHi  = sIPCBufHi;
        sIPCBufLo  = sIPCBufLo;
        sIPCInitialized = TRUE;
    }
}

// ---------------------------------------------------------------------------
// IPCReadReg / IPCWriteReg
//   Direct 32-bit reads/writes to the IPC hardware registers.
//   reg 0 = PPCMSG, 1 = PPCCTRL, etc. (0xCD000000 + reg*4)
// ---------------------------------------------------------------------------
u32 IPCReadReg(int reg) {
    return *(vu32*)(IPC_BASE_ADDR + (u32)reg * 4);
}

void IPCWriteReg(int reg, u32 val) {
    *(vu32*)(IPC_BASE_ADDR + (u32)reg * 4) = val;
}

// ---------------------------------------------------------------------------
// strnlen  (used internally for IOS path copy)
// ---------------------------------------------------------------------------
int strnlen(const char* s, int n) {
    const char* p = s;
    while (*p != '\0' && n-- != 0) {
        p++;
    }
    return (int)(p - s);
}

// ---------------------------------------------------------------------------
// IPCInterruptHandler
//   Called from the OS exception dispatcher when the IPC hardware interrupt
//   fires (exception 0x1B).  Handles both "PPC message sent" and "ARM->PPC
//   reply ready" cases.
// ---------------------------------------------------------------------------
static void IPCInterruptHandler(s16 exception, void* ctx) {
    u32 ctrl;

    // Case 1: ARM acknowledged our last send (Y1 bit set alongside X1)
    ctrl = IPCReadReg(IPC_REG_PPCCTRL);
    if ((ctrl & (IPC_CTRL_Y1 | IPC_CTRL_X1)) == (IPC_CTRL_Y1 | IPC_CTRL_X1)) {
        // Deliver the reply through the async path
        // (FUN_80221a28 in the binary)
    }

    // Case 2: ARM sent us a new message (Y2 bit set alongside X2)
    ctrl = IPCReadReg(IPC_REG_PPCCTRL);
    if ((ctrl & (IPC_CTRL_X2 | IPC_CTRL_Y2)) == (IPC_CTRL_X2 | IPC_CTRL_Y2)) {
        // ACK the ARM message: clear Y2, set IY2 flag
        IPCWriteReg(IPC_REG_PPCCTRL, (ctrl & 0x30) | IPC_CTRL_Y2);
        // Acknowledge via the interrupt ACK register
        *(vu32*)(IPC_BASE_ADDR + 0x30) = 0x40000000;

        sIPCSendCount++;

        // If a sync waiter is blocked, wake it with the reply
        if (sIPCSendCount > 0) {
            if (sIPCSyncRequest != NULL) {
                *(s32*)((u8*)sIPCSyncQueue + 4) = 0;
                sIPCSyncRequest = NULL;
                OSWakeupThread((OSThreadQueue*)((u8*)sIPCSyncQueue + 0x2c));
                ctrl = IPCReadReg(IPC_REG_PPCCTRL);
                IPCWriteReg(IPC_REG_PPCCTRL, (ctrl & 0x30) | IPC_CTRL_X2);
            }

            // If there are pending queued requests, dispatch the next one
            if (sIPCQueueTail != sIPCQueueHead) {
                if (sIPCQueueTail - sIPCQueueHead != 0) {
                    void* req = sIPCRequestQueue[sIPCQueueOut];
                    if (req != NULL) {
                        if (*(s32*)((u8*)req + 0x28) != 0) {
                            sIPCSendCount--;
                        }
                        // Dispatch: write physical address of request to PPCMSG
                        IPCWriteReg(IPC_REG_PPCMSG, (u32)req - 0x80000000);
                        sIPCSendCount--;
                        sIPCQueueHead++;
                        sIPCQueueOut = (sIPCQueueOut + 1) & IPC_QUEUE_MASK;
                        ctrl = IPCReadReg(IPC_REG_PPCCTRL);
                        IPCWriteReg(IPC_REG_PPCCTRL, (ctrl & 0x30) | IPC_CTRL_X1);
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// __ios_Ipc2
//   Core IPC send routine.  Enqueues an IOS request block onto the ring
//   buffer and optionally blocks (param_2 == 0 -> synchronous).
// ---------------------------------------------------------------------------
static s32 __ios_Ipc2(void* req, s32 async) {
    u32  ctrl;
    BOOL saved;
    s32  result;

    if (req == NULL) {
        return IOS_EINVAL;
    }

    // For synchronous calls, initialise a thread queue inside the request
    if (async == 0) {
        OSInitThreadQueue((OSThreadQueue*)((u8*)req + 0x2c));
    }

    // Flush the request block to RAM so the ARM can see it
    DCFlushRange(req, 0x20);

    saved  = OSDisableInterrupts();
    result = 0;

    // Check if the ring buffer is full
    if (sIPCQueueTail - sIPCQueueHead == IPC_QUEUE_SIZE) {
        result = IOS_EQUEUEFULL;
    } else {
        sIPCRequestQueue[sIPCQueueIn] = req;
        sIPCQueueIn  = (sIPCQueueIn  + 1) & IPC_QUEUE_MASK;
        sIPCQueueTail++;
        IPCiProfQueueReq(req, *(u32*)((u8*)req + 8));
    }

    if (result == 0) {
        // If the hardware is ready to accept a new command, dispatch immediately
        if (sIPCSendCount > 0) {
            if (sIPCQueueTail - sIPCQueueHead != 0) {
                void* next = sIPCRequestQueue[sIPCQueueOut];
                if (next != NULL) {
                    if (*(s32*)((u8*)next + 0x28) != 0) {
                        sIPCSendCount--;
                    }
                    IPCWriteReg(IPC_REG_PPCMSG, (u32)next - 0x80000000);
                    sIPCSendCount--;
                    sIPCQueueHead++;
                    sIPCQueueOut = (sIPCQueueOut + 1) & IPC_QUEUE_MASK;
                    ctrl = IPCReadReg(IPC_REG_PPCCTRL);
                    IPCWriteReg(IPC_REG_PPCCTRL, (ctrl & 0x30) | IPC_CTRL_X1);
                }
            }
        }

        if (async == 0) {
            // Block until the ARM delivers a reply
            OSSleepThread((OSThreadQueue*)((u8*)req + 0x2c));
        }
        OSRestoreInterrupts(saved);

        if (async == 0) {
            result = *(s32*)((u8*)req + 4);
        }
    } else {
        OSRestoreInterrupts(saved);
        if (async != 0) {
            iosFree(sIPCHeap, req);
        }
    }

    // Sync callers also free the request block after reading the result
    if (req != NULL && async == 0) {
        iosFree(sIPCHeap, req);
    }

    return result;
}

// ---------------------------------------------------------------------------
// IPCCltInit
//   Initialises the IPC client layer: allocates the request heap, registers
//   the interrupt handler, and enables the IPC hardware interrupt.
//   Returns 0 on success or a negative IOS error code.
// ---------------------------------------------------------------------------
s32 IPCCltInit(void) {
    u32 lo, hi;
    s32 heap;

    if (sIPCCltInitialized) {
        return 0;
    }

    sIPCCltInitialized = TRUE;

    IPCInit();

    lo = IPCGetBufferLo();
    hi = IPCGetBufferHi();

    if (hi < lo + 0x1000) {
        return IOS_ENOMEM;
    }

    heap = iosCreateHeap((void*)lo, 0x1000);
    sIPCHeap = heap;
    IPCSetBufferLo(lo + 0x1000);

    OSSetExceptionHandler(0x1B, (OSInterruptHandler)IPCInterruptHandler);
    __OSUnmaskInterrupts(0x10);
    IPCWriteReg(IPC_REG_PPCCTRL, IPC_CTRL_IY1 | IPC_CTRL_IY2 | IPC_CTRL_X2);

    IPCiProfInit();
    OSCreateAlarm(&sIPCSendCount);   // NOTE: reuses sIPCSendCount field in original

    return 0;
}

// ---------------------------------------------------------------------------
// IOS_Open  (synchronous)
//   Opens an IOS device/file by path with the given access mode.
//   Returns a file descriptor >= 0, or a negative error code.
// ---------------------------------------------------------------------------
s32 IOS_Open(const char* path, u32 mode) {
    void*  req;
    int    pathLen;
    s32    result;

    result = 0;

    req = iosAllocAligned(sIPCHeap, 0x40, 0x20);
    if (req == NULL) {
        return IOS_ENOMEM;
    }

    // Initialise callback/mode fields to "synchronous, no callback"
    *(u32*)((u8*)req + 0x20) = 0;
    *(u32*)((u8*)req + 0x24) = 0;
    *(u32*)((u8*)req + 0x28) = 0;
    // Command: IOS_CMD_OPEN
    *(u32*)((u8*)req +  0x00) = IOS_CMD_OPEN;
    *(u32*)((u8*)req +  0x08) = mode;

    // Flush the path to RAM and store its physical address in the request
    pathLen = strnlen(path, 0x40) + 1;
    DCFlushRange((void*)path, pathLen);
    *(u32*)((u8*)req + 0x0C) = (u32)path - 0x80000000;
    *(u32*)((u8*)req + 0x10) = mode;

    result = __ios_Ipc2(req, 0 /* synchronous */);
    return result;
}

// ---------------------------------------------------------------------------
// IOS_OpenAsync  (asynchronous)
//   Non-blocking variant.  Calls callback(result, usrData) when complete.
// ---------------------------------------------------------------------------
void IOS_OpenAsync(const char* path, u32 mode, IOSAsyncCallback callback, void* usrData) {
    void*  req;
    int    pathLen;
    s32    result;

    result = 0;

    req = iosAllocAligned(sIPCHeap, 0x40, 0x20);
    if (req == NULL) {
        result = IOS_ENOMEM;
    } else {
        *(u32*)((u8*)req + 0x20) = (u32)callback;
        *(u32*)((u8*)req + 0x24) = (u32)usrData;
        *(u32*)((u8*)req + 0x28) = 0;
        *(u32*)((u8*)req +  0x00) = IOS_CMD_OPEN;
        *(u32*)((u8*)req +  0x08) = 0;
    }

    if (result == 0) {
        if (req == NULL) {
            result = IOS_EINVAL;
        } else {
            pathLen = strnlen(path, 0x40) + 1;
            DCFlushRange((void*)path, pathLen);
            *(u32*)((u8*)req + 0x0C) = (u32)path - 0x80000000;
            *(u32*)((u8*)req + 0x10) = (u32)usrData;
        }

        if (result == 0) {
            result = __ios_Ipc2(req, (s32)callback /* non-zero -> async */);
        }
    }

    // Deliver error synchronously via callback if setup failed
    if (callback != NULL) {
        callback(result, usrData);
    }
}

// ---------------------------------------------------------------------------
// IOS_IoctlAsync  (asynchronous ioctl)
// ---------------------------------------------------------------------------
void IOS_IoctlAsync(s32 fd, u32 ioctl, void* inBuf, u32 inLen,
                    void* ioBuf, u32 ioLen,
                    IOSAsyncCallback callback, void* usrData) {
    void* req;
    s32   result;

    req    = iosAllocAligned(sIPCHeap, 0x40, 0x20);
    result = 0;

    if (req == NULL) {
        result = IOS_ENOMEM;
    } else {
        *(u32*)((u8*)req + 0x20) = (u32)callback;
        *(u32*)((u8*)req + 0x24) = (u32)usrData;
        *(u32*)((u8*)req + 0x28) = 0;
        *(u32*)((u8*)req +  0x00) = 6;  // IOS_CMD_IOCTL
        *(u32*)((u8*)req +  0x08) = (u32)callback;
    }

    if (result == 0) {
        if (req == NULL) {
            result = IOS_EINVAL;
        } else {
            *(u32*)((u8*)req + 0x0C) = (u32)usrData;

            // Store input buffer physical address (or NULL)
            *(u32*)((u8*)req + 0x18) = (ioBuf ? (u32)ioBuf - 0x80000000 : 0);
            *(u32*)((u8*)req + 0x1C) = ioLen;
            *(u32*)((u8*)req + 0x10) = (inBuf ? (u32)inBuf - 0x80000000 : 0);
            *(u32*)((u8*)req + 0x14) = inLen;

            // Flush both buffers to RAM for ARM visibility
            if (inBuf) DCFlushRange(inBuf, inLen);
            if (ioBuf) DCFlushRange(ioBuf, ioLen);
        }

        if (result == 0) {
            result = __ios_Ipc2(req, (s32)callback);
        }
    }

    if (callback != NULL) {
        callback(result, usrData);
    }
}

// ---------------------------------------------------------------------------
// IOS_Ioctlv  (synchronous ioctlv -- scatter/gather variant)
// ---------------------------------------------------------------------------
void IOS_Ioctlv(s32 fd, u32 ioctl, u32 cnt, u32 cntOut, void* vec) {
    void* req;
    s32   result;

    req    = iosAllocAligned(sIPCHeap, 0x40, 0x20);
    result = 0;

    if (req == NULL) {
        result = IOS_ENOMEM;
    } else {
        *(u32*)((u8*)req + 0x20) = 0;
        *(u32*)((u8*)req + 0x24) = 0;
        *(u32*)((u8*)req + 0x28) = 0;
        *(u32*)((u8*)req +  0x00) = IOS_CMD_IOCTLV;
        *(u32*)((u8*)req +  0x08) = (u32)ioctl;  // register-reuse from call site; field mirrors mode/ioctl hi
    }

    if (result == 0) {
        // FUN_802231b8 fills in the ioctlv vector fields
        // result = FUN_802231b8(req, fd, cnt, cntOut, vec);
    }

    if (result == 0) {
        result = __ios_Ipc2(req, 0 /* synchronous */);
    }
}

// ---------------------------------------------------------------------------
// IPCiProfInit / IPCiProfQueueReq
//   Lightweight IPC profiling ring buffers -- two arrays of 32 slots each.
// ---------------------------------------------------------------------------
#define IPC_PROF_SIZE  32

static u32  sIPCProfReq[IPC_PROF_SIZE];   // [8065F120] last N request pointers
static u32  sIPCProfCmd[IPC_PROF_SIZE];   // [8065F1A0] last N commands
static u32  sIPCProfIdx;                  // [r13 - 0x2948]
static u32  sIPCProfCount;                // [r13 - 0x2944]

void IPCiProfInit(void) {
    u32 i;
    sIPCProfIdx   = 0;
    sIPCProfCount = 0;
    for (i = 0; i < IPC_PROF_SIZE; i++) {
        sIPCProfCmd[i] = 0;
        sIPCProfReq[i] = 0xFFFFFFFF;
    }
}

void IPCiProfQueueReq(void* req, u32 cmd) {
    u32 i;
    sIPCProfIdx++;
    sIPCProfCount++;
    for (i = 0; i < IPC_PROF_SIZE; i++) {
        if (sIPCProfCmd[i] == 0 && sIPCProfReq[i] == 0xFFFFFFFF) {
            sIPCProfCmd[i] = (u32)req;
            sIPCProfReq[i] = cmd;
            return;
        }
    }
}
