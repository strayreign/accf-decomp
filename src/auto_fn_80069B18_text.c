#include <dolphin/types.h>

extern void fn_8044E450(void*);
extern void fn_80068D4C(void*);
extern void fn_800860D0(void*);
extern void fn_800693BC(void*);
extern void __register_global_object(void*, void*, void*);

extern void fn_80069A58(void);
extern void fn_80069C7C(void);
extern void fn_80069CC8(void);
extern void fn_800699F8(void);
extern void fn_80069998(void);

extern u8 lbl_8074E458;
extern u8 lbl_80574960[0xAC];
extern void* lbl_804A5678;

void fn_80069B18(void) {
    u8* base = (u8*)&lbl_80574960[0];
    
    lbl_8074E458 = 0x00;
    
    fn_8044E450(base + 0xac);
    fn_80068D4C(base + 0xac);
    __register_global_object(base + 0xac, (void*)fn_80069CC8, base + 0xa0);
    
    fn_800860D0(base + 0x16f4);
    *(void**)((u8*)(base + 0x16f4) + 0x04) = &lbl_804A5678;
    *(u8*)((u8*)(base + 0x16f4) + 0x56) = 0x00;
    __register_global_object(base + 0x16f4, (void*)fn_80069A58, base + 0x16e8);
    
    fn_800693BC(base + 0x1774);
    __register_global_object(base + 0x1774, (void*)fn_800699F8, base + 0x1768);
}
