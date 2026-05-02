#include <dolphin/types.h>

extern void fn_800860D0(void *);
extern void fn_800A77A4(void *);
extern void mTex_initDesc(void *, int, int, int, int);
extern void mTex_edit8b_c_init(void *, int, int, int);
extern void __register_global_object(void *, void *, void *);
extern void dFootmark_Editor_c_dtor(void);

extern u8 lbl_804E5DCC[];
extern u8 lbl_804E5E38[];
extern u8 lbl_804E5E98[];
extern u8 lbl_80524478[];
extern u8 lbl_80591BE0[];
extern u8 lbl_80591C00[];
extern u8 lbl_8074E600;

void fn_800A8368(void) {
    u8 *base = lbl_80591C00;
    u8 *texdesc;

    lbl_8074E600 = 0;
    fn_800860D0((void *)base);

    *(u32 *)((u8 *)base + 0x04) = (u32)lbl_804E5E38;
    texdesc = base + 0x58;
    *(u32 *)((u8 *)base + 0x78) = (u32)lbl_804E5E98;

    mTex_initDesc((void *)texdesc, 0x04, 0x04, 0x04, 0x04);

    *(u32 *)((u8 *)texdesc + 0x20) = (u32)lbl_80524478;
    mTex_edit8b_c_init((void *)texdesc, 0x00, 0x00, 0x00);

    *(u32 *)((u8 *)base + 0x5084) = 0;
    *(u32 *)((u8 *)base + 0x04) = (u32)lbl_804E5DCC;
    *(u32 *)((u8 *)base + 0x78) = (u32)((u8 *)lbl_804E5DCC + 0x14);

    fn_800A77A4((void *)base);

    __register_global_object((void *)base, (void *)dFootmark_Editor_c_dtor, (void *)lbl_80591BE0);
}
