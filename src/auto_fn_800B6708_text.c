#include <dolphin/types.h>

extern u8 lbl_805984B0[];
extern u8 lbl_805984C0[];

extern void fn_800B6350(void *);
extern void fn_800B6384(void);
extern void __register_global_object(void *, void *, void *);

static void fn_800B6708(void)
{
    fn_800B6350(lbl_805984C0);
    __register_global_object(lbl_805984C0, (void *)fn_800B6384, lbl_805984B0);
}

__declspec(section ".ctors") static void (*const _ctor_fn_800B6708)(void) = fn_800B6708;
