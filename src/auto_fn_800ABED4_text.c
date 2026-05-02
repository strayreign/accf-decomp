#include <dolphin/types.h>

extern void fn_800A9064(void *);
extern void fn_800AB1C0(void *);
extern void fn_800ABF78(void);
extern void fn_800ABFA8(void);
extern void fn_800AC00C(void);
extern void fn_800AC010(void);
extern void fn_800AC050(void);
extern void fn_800AC0B4(void);
extern void fn_8044E450(void *, void *, void *, s32, s32);
extern void __register_global_object(void *, void *, void *);

extern u8 lbl_80596CA0[];

static void fn_800ABED4(void);

static void fn_800ABED4(void)
{
    fn_8044E450((u8*)lbl_80596CA0 + 0x10, (void *)fn_800ABF78, (void *)fn_800AC0B4, 0x14, 4);
    __register_global_object((u8*)lbl_80596CA0 + 0x10, (void *)fn_800ABFA8, (void *)lbl_80596CA0);
    fn_800A9064((u8*)lbl_80596CA0 + 0x260);
    fn_8044E450((u8*)lbl_80596CA0 + 0x370, (void *)fn_800AC00C, (void *)fn_800AC010, 0x3c, 2);
    fn_800AB1C0((u8*)lbl_80596CA0 + 0x370);
    __register_global_object((u8*)lbl_80596CA0 + 0x370, (void *)fn_800AC050, (u8*)lbl_80596CA0 + 0x360);
}

__declspec(section ".ctors") static void (*const ctor_fn_800ABED4)(void) = fn_800ABED4;
