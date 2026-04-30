#include <dolphin/types.h>

typedef struct {
    u8 field_0x0;
    u8 field_0x1;
    u8 field_0x2;
    u8 field_0x3;
    u8 field_0x4;
    u8 field_0x5;
    u8 field_0x6;
    u8 field_0x7;
    u8 field_0x8;
    u8 field_0x9;
    u8 field_0xA;
    u8 field_0xB;
    u8 field_0xC;
    u8 field_0xD;
    u8 field_0xE;
    u8 field_0xF;
} SomeStruct;

extern void fn_8044E450(SomeStruct* param1, u32 param2, u32 param3);

void fn_80082024(void) {
    SomeStruct* ptr = (SomeStruct*)lbl_805838D8;
    u32 val1 = 0x0;
    u32 val2 = 0x1c;
    u32 val3 = 0x20;

    fn_8044E450(ptr, val1, val2);
}
