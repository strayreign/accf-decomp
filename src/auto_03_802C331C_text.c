#include <dolphin/types.h>

extern void* memset(void*, int, u32);

void* memset_802C331C(void* ptr) {
    return memset(ptr, 0, 0x9C400);
}
