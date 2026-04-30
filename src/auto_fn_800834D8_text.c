#include <dolphin/types.h>

extern void fn_8010F248(void*);
extern u8 lbl_80583CE0[];
extern u8 lbl_80584560[];

void fn_800834D8(void) {
    fn_8010F248(lbl_80583CE0);
    fn_8010F248(lbl_80584560);
}
