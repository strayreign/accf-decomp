#include <dolphin/types.h>

typedef struct {
    void *next;
    void *prev;
    u8 pad_08[0x0c];
    u8 field_14;
    u8 field_15;
    u8 field_16;
    u8 field_17;
    u8 field_18;
} ColorFader;

extern void fn_802AB844(void *a, void *b);
extern ColorFader lbl_805838D8[32];

void fn_80081F04(ColorFader *fader) {
    *(u8*)((u8*)fader + 0x14) = 0;
    *(u32*)((u8*)fader + 0x0c) = 0;
    *(u32*)((u8*)fader + 0x10) = 0;
    *(u8*)((u8*)fader + 0x15) = 0;
    *(u8*)((u8*)fader + 0x16) = 0;
    *(u8*)((u8*)fader + 0x17) = 0;
    *(u8*)((u8*)fader + 0x18) = 0;
}

void fn_80081F28(ColorFader *parent) {
    ColorFader *current;
    ColorFader *next;
    
    current = (ColorFader*)*(u32*)((u8*)parent + 0x00);
    
    while (current != NULL) {
        next = (ColorFader*)*(u32*)((u8*)current + 0x04);
        fn_802AB844(parent, current);
        fn_80081F04(current);
        current = next;
    }
}

void fn_80081F8C(void) {
    ColorFader *current;
    u32 idx;
    
    current = &lbl_805838D8[0];
    idx = 0;
    
    while (idx < 0x20) {
        fn_80081F04(current);
        idx = idx + 1;
        current = (ColorFader*)((u8*)current + 0x1c);
    }
}

ColorFader *fn_80081FDC(void) {
    u32 idx;
    ColorFader *current;
    
    current = &lbl_805838D8[0];
    idx = 0;
    
    while (idx < 0x20) {
        if (*(u8*)((u8*)current + 0x14) == 0) {
            return (ColorFader*)((u8*)&lbl_805838D8[0] + (idx * 0x1c));
        }
        current = (ColorFader*)((u8*)current + 0x1c);
        idx = idx + 1;
    }
    
    return NULL;
}
