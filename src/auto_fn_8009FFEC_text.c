#include <dolphin/types.h>

extern void fn_800929E8(void *);
extern void fn_800A5C84(void *, void *, void *);
extern void fn_800A5CC4(void *, void *, void *);
extern void fn_800A5D20(void *, void *, void *);
extern void __register_global_object(void *, void *, void *);

extern u8 lbl_80588FD8[];

void fn_8009FFEC(void) {
    *(u32*)((u8*)lbl_80588FD8 + 0x330) = 0;
    *(u32*)((u8*)lbl_80588FD8 + 0x334) = 0;
    fn_800929E8((u8*)lbl_80588FD8 + 0x35C);
    fn_800A5C84((u8*)lbl_80588FD8 + 0x344, (void*)0, (u8*)lbl_80588FD8 + 0x338);
    *(u8*)((u8*)lbl_80588FD8 + 0xE24) = 0;
    fn_800A5D20(0, (void*)0, (u8*)lbl_80588FD8 + 0x1128);
}
