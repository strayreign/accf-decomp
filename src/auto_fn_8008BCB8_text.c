#include <dolphin/types.h>

extern u8 lbl_8074E568[8];

void fn_8008BCB8(void)
{
    *(u32*)((u8*)lbl_8074E568 + 0) = 0;
    *(u32*)((u8*)lbl_8074E568 + 4) = 0;
}
