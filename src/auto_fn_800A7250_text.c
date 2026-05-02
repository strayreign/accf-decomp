#include <dolphin/types.h>

extern void fn_800860D0(void *);
extern void fn_800A71F4(void);
extern void fn_800A7250(void);
extern void fn_800A72CC(void);
extern void __register_global_object(void *, void *, void *);

extern u8 lbl_8058B888[];
extern void *lbl_804E5CF4;
extern void *lbl_804E5D28;

void fn_800A7250(void)
{
    u8 *base = lbl_8058B888;
    
    /* Initialize first object at offset 0x10 */
    fn_800860D0((void *)(base + 0x10));
    *(void **)((u8*)(base + 0x10) + 0x04) = lbl_804E5D28;
    __register_global_object((void *)(base + 0x10), (void *)fn_800A72CC, (void *)base);
    
    /* Initialize second object at offset 0x78 */
    fn_800860D0((void *)(base + 0x78));
    *(void **)((u8*)(base + 0x78) + 0x04) = lbl_804E5CF4;
    __register_global_object((void *)(base + 0x78), (void *)fn_800A71F4, (void *)(base + 0x68));
}
