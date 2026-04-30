#include <dolphin/types.h>

extern void fn_8000DD58(void);
extern void __register_global_object(void* sda_addr, void* fn_addr, void* obj_addr);

extern s16 lbl_8074E5F8;
extern void lbl_8058B878;

void fn_800A7070(void) {
    lbl_8074E5F8 = (s16)0xFFF1;
    __register_global_object(&lbl_8074E5F8, (void*)&fn_8000DD58, (void*)&lbl_8058B878);
}
