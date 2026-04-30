#include <dolphin/types.h>

extern u32 lbl_8074E368;

s32 fn_80455C9C(void) {
    lbl_8074E368 = lbl_8074E368 * 0x41c64e6d + 0x3039;
    return (lbl_8074E368 >> 16) & 0x7fff;
}

void fn_80455CBC(u32 seed) {
    lbl_8074E368 = seed;
}
