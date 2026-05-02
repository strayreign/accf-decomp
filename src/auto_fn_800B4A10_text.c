#include <dolphin/types.h>

extern void fn_80117524(void *);
extern void fn_80117534(void);
extern void __register_global_object(void *, void *, void *);
extern void fn_8044E450(void *, void *, u32, u32, u32);
extern void *memset(void *, s32, s32);
extern void fn_800B24F8(void *);
extern void fn_800B2700(void);
extern void fn_800B4BA0(void);

extern u8 lbl_80597220[];

void fn_800B4A10(void)
{
    u8 *base;
    u8 *ptr;
    u32 offset;
    u32 i;

    base = lbl_80597220;

    fn_80117524(base + 0x10);
    __register_global_object(base + 0x10, (void *)fn_80117534, base);

    fn_8044E450(base + 0x3a0, (void *)fn_800B4BA0, 0, 4, 4);

    memset(base + 0x3b0, 0, 0x210);
    memset(base + 0x5c0, 0, 0x200);

    ptr = base + 0x7c0;
    offset = 0;
    
    for (i = 0; i < 4; i++) {
        *(u32*)(ptr + 0x00) = 0;
        *(u32*)(ptr + 0x04) = 0;
        *(u32*)(ptr + 0x08) = 0;
        *(u32*)(ptr + 0x0c) = 0;
        *(u32*)(ptr + 0x10) = 0;
        *(u32*)(ptr + 0x14) = 0;
        *(u32*)(ptr + 0x18) = 0;
        *(u32*)(ptr + 0x1c) = 0;
        *(u32*)(ptr + 0x20) = 0;
        *(u32*)(ptr + 0x24) = 0;
        *(u32*)(ptr + 0x28) = 0;
        *(u32*)(ptr + 0x2c) = 0;
        *(u32*)(ptr + 0x30) = 0;
        *(u32*)(ptr + 0x34) = 0;
        *(u32*)(ptr + 0x38) = 0;
        *(u32*)(ptr + 0x3c) = 0;
        *(u32*)(ptr + 0x40) = 0;
        *(u32*)(ptr + 0x44) = 0;
        *(u32*)(ptr + 0x48) = 0;
        *(u32*)(ptr + 0x4c) = 0;
        *(u32*)(ptr + 0x50) = 0;
        *(u32*)(ptr + 0x54) = 0;
        *(u32*)(ptr + 0x58) = 0;
        *(u32*)(ptr + 0x5c) = 0;
        *(u32*)(ptr + 0x60) = 0;
        *(u32*)(ptr + 0x64) = 0;
        *(u32*)(ptr + 0x68) = 0;
        *(u32*)(ptr + 0x6c) = 0;
        *(u32*)(ptr + 0x70) = 0;
        *(u32*)(ptr + 0x74) = 0;
        *(u32*)(ptr + 0x78) = 0;
        *(u32*)(ptr + 0x7c) = 0;
        
        offset += 0x20;
        ptr += 0x80;
    }

    *(u32*)((u8*)(base + 0x7c0) + (offset << 2)) = 0;
    *(u32*)((u8*)(base + 0x7c0) + (offset << 2) + 0x04) = 0;
    *(u32*)((u8*)(base + 0x7c0) + (offset << 2) + 0x08) = 0;
    *(u32*)((u8*)(base + 0x7c0) + (offset << 2) + 0x0c) = 0;
    *(u32*)(base + 0x7c0 + 0x210) = 0;

    fn_800B24F8(base + 0x1254);
    fn_800B24F8(base + 0x1260);
    fn_800B24F8(base + 0x126c);
    fn_800B24F8(base + 0x1278);
    fn_800B24F8(base + 0x1284);

    __register_global_object(base + 0x1254, (void *)fn_800B2700, base + 0x1248);
}
