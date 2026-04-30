#include <dolphin/types.h>
#include <egg/ColorFader.h>

void fn_804526B4(EGG_ColorFader *self) {
    u32 temp = *(u32 *)((u8 *)self + 0x18);
    *(u32 *)((u8 *)self + 0x24) = *(u32 *)((u8 *)self + 0x1c);
    *(s32 *)((u8 *)self + 0x28) = *(s32 *)((u8 *)self + 0x20) - (s32)(temp & *(u32 *)((u8 *)self + 0x2c));
    *(u32 *)((u8 *)self + 0x34) = temp;
}
