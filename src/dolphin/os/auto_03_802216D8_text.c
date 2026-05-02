#include <dolphin/os/IPC.h>

#define IPC_BASE_ADDR 0xCD000000

u32 IPCGetBufferHi(void) {
    return *(vu32*)IPC_BASE_ADDR;
}

u32 IPCGetBufferLo(void) {
    return *(vu32*)(IPC_BASE_ADDR + 4);
}

void IPCSetBufferLo(u32 lo) {
    *(vu32*)(IPC_BASE_ADDR + 4) = lo;
}
