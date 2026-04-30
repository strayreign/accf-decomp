#include <dolphin/types.h>

s32 fn_802C6310(s32 a, s32 b) {
    s32 stride = b * 0x4C + 8;
    return stride * (a - 1);
}
