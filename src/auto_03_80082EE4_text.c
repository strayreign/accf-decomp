#include <dolphin/types.h>

extern void fn_80440138(void);
extern void *fn_801017B8(void);
extern void *fn_80101894(void);
extern void *fn_8010E308(void);
extern u32 fn_8013E128(void *ptr);
extern u32 fn_8013E1CC(void *ptr, u32 val);
extern void fn_801480A4(void *ptr, u32 idx);
extern u32 fn_800DCEDC(void);
extern u32 fn_800DCF30(void);
extern void fn_800DCF58(void);
extern void fn_800DD4C8(void);
extern void fn_800DD518(void *ptr, u32 size);
extern void fn_800DD588(u32 val, u32 size);
extern void memcpy(void *dst, void *src, u32 size);
extern void DCStoreRangeNoSync(void *ptr, u32 size);

static u8 lbl_80583CE0[0x880];
static u8 lbl_80584560[0x880];
extern u32 lbl_8074A0AC;
extern u32 lbl_8074A0A8;
extern u8 lbl_807507E8;
extern u8 lbl_807507E9;

void fn_80082EE4(void *ptr, u32 val) {
    if (ptr != 0 && val > 0) {
        fn_80440138();
    }
}

void *fn_80082F24(void) {
    return (void *)lbl_80583CE0;
}

void *fn_80082F30(u32 idx, u32 param) {
    void *result = 0;
    
    switch (idx) {
        case 0: {
            void *base = fn_801017B8();
            if (base != 0) {
                u32 offset = (param & 7) * 0x880;
                result = (void *)((u8 *)base + offset + 0x1140);
            }
            break;
        }
        case 4: {
            void *base = fn_8010E308();
            u32 masked_param = param & 7;
            result = (void *)((u8 *)base + 0x60000 - 0x1380);
            fn_801480A4(result, masked_param);
            break;
        }
        case 5: {
            void *base = fn_8010E308();
            result = (void *)((u8 *)base + 0x60000 - 0x1da0);
            break;
        }
        case 6:
            return fn_80082F24();
        case 7: {
            void *base = fn_8010E308();
            void *addr = (void *)((u8 *)base + 0x70000 - 0x2a40);
            u32 val = fn_8013E128(addr);
            if (fn_8013E1CC(addr, val) == 0) {
                result = 0;
            } else {
                result = addr;
            }
            break;
        }
        case 8: {
            void *base = fn_80101894();
            if (base != 0) {
                u32 offset = (param & 7) * 0x880;
                result = (void *)((u8 *)base + offset + 0x1140);
            }
            break;
        }
    }
    
    return result;
}

u32 fn_80083044(void *arg1, u32 idx1, void *arg2, u32 idx2, void *arg3, u32 idx3) {
    void *src = fn_80082F30(idx1, idx1);
    void *dst = fn_80082F30(idx2, idx2);
    
    if (src == 0 || dst == 0) {
        return 0;
    }
    
    memcpy(dst, src, 0x880);
    DCStoreRangeNoSync(dst, 0x880);
    
    if (idx3 != 0) {
        if (fn_800DCEDC() == 0) {
            fn_800832C8();
            return 0;
        }
        
        u32 dcf_val = fn_800DCF30();
        if (dcf_val <= 1) {
            fn_800832C8();
            return 0;
        }
        
        if (fn_800832E8() == 0) {
            return 0;
        }
        
        fn_800DCF58();
        
        u32 ctrl_val = 0;
        ctrl_val |= (idx1 & 0xF) << 28;
        ctrl_val |= (idx1 & 0x3FF) << 18;
        ctrl_val |= (idx2 & 0x3FF) << 14;
        ctrl_val |= (idx3 & 0x3FF) << 4;
        
        u32 bit_pos = fn_800DCF58();
        lbl_8074A0AC = (1u << bit_pos);
        ctrl_val &= ~0x04;
        
        fn_800DD4C8();
        fn_800DD518((void *)&ctrl_val, 4);
        fn_800DD588(0x3E, 4);
    } else {
        fn_800832C8();
    }
    
    return 1;
}

u32 fn_8008316C(void *arg1, u32 idx1, void *arg2, u32 idx2, void *arg3, u32 idx3) {
    void *src = fn_80082F30(idx1, idx1);
    void *dst = fn_80082F30(idx2, idx2);
    
    if (src == 0 || dst == 0) {
        return 0;
    }
    
    u8 tmp[0x880];
    memcpy(tmp, src, 0x880);
    memcpy(src, dst, 0x880);
    memcpy(dst, tmp, 0x880);
    
    DCStoreRangeNoSync(src, 0x880);
    DCStoreRangeNoSync(dst, 0x880);
    
    if (idx3 != 0) {
        if (fn_800DCEDC() == 0) {
            fn_800832D8();
            return 0;
        }
        
        u32 dcf_val = fn_800DCF30();
        if (dcf_val <= 1) {
            fn_800832D8();
            return 0;
        }
        
        if (fn_80083358() == 0) {
            return 0;
        }
        
        fn_800DCF58();
        
        u32 ctrl_val = 0;
        ctrl_val |= (idx1 & 0xF) << 28;
        ctrl_val |= (idx1 & 0x3FF) << 18;
        ctrl_val |= (idx2 & 0x3FF) << 14;
        ctrl_val |= (idx3 & 0x3FF) << 4;
        
        u32 bit_pos = fn_800DCF58();
        lbl_8074A0A8 = (1u << bit_pos);
        ctrl_val |= 0x08;
        
        fn_800DD4C8();
        fn_800DD518((void *)&ctrl_val, 4);
        fn_800DD588(0x3E, 4);
    } else {
        fn_800832D8();
    }
    
    return 1;
}

void fn_800832C8(void) {
    lbl_8074A0AC = 0xFFFFFFFF;
}

void fn_800832D8(void) {
    lbl_8074A0A8 = 0xFFFFFFFF;
}

u32 fn_800832E8(void) {
    if (fn_800DCEDC() == 0) {
        return 1;
    }
    
    u32 val = lbl_8074A0AC;
    u32 bit0 = val & 1;
    u32 bit29 = (val >> 29) & 1;
    u32 bit30 = (val >> 30) & 1;
    u32 bit28 = (val >> 28) & 1;
    
    u32 sum = bit28 + bit0 + bit29 + bit30;
    u32 dcf_val = fn_800DCF30();
    
    u32 diff = sum - dcf_val;
    u32 combined = sum | dcf_val;
    u32 shifted = diff >> 1;
    u32 result_val = shifted - combined;
    
    return (result_val >> 31) & 1;
}

u32 fn_80083358(void) {
    if (fn_800DCEDC() == 0) {
        return 1;
    }
    
    u32 val = lbl_8074A0A8;
    u32 bit0 = val & 1;
    u32 bit29 = (val >> 29) & 1;
    u32 bit30 = (val >> 30) & 1;
    u32 bit28 = (val >> 28) & 1;
    
    u32 sum = bit28 + bit0 + bit29 + bit30;
    u32 dcf_val = fn_800DCF30();
    
    u32 diff = sum - dcf_val;
    u32 combined = sum | dcf_val;
    u32 shifted = diff >> 1;
    u32 result_val = shifted - combined;
    
    return (result_val >> 31) & 1;
}

void fn_800833C8(u32 idx) {
    if (idx == 7) {
        fn_800DCF58();
    }
}

void fn_800833D8(u32 *data_ptr, u32 param) {
    u32 data = *data_ptr;
    
    u32 nib3 = (data >> 28) & 0xF;
    u32 field4 = (data >> 4) & 0x3FF;
    u32 field5 = (data >> 14) & 0xF;
    u32 field6 = (data >> 18) & 0x3FF;
    
    u32 bit28 = (data >> 28) & 1;
    
    if (bit28 != 0) {
        fn_8008316C(0, nib3, 0, field4, 0, field5);
        fn_800DD4C8();
        fn_800DD518((void *)&lbl_807507E8, 1);
        fn_800DD588(0x3F, param);
    } else {
        fn_80083044(0, nib3, 0, field4, 0, field5);
        fn_800DD4C8();
        fn_800DD518((void *)&lbl_807507E9, 1);
        fn_800DD588(0x3F, param);
    }
}

void fn_80083468(u32 flag, u32 bit_pos) {
    if (flag != 0) {
        u32 val = lbl_8074A0AC;
        val |= (1u << bit_pos);
        lbl_8074A0AC = val;
        
        if (fn_800832E8() == 0) {
            fn_800832C8();
        }
    } else {
        u32 val = lbl_8074A0A8;
        val |= (1u << bit_pos);
        lbl_8074A0A8 = val;
        
        if (fn_80083358() == 0) {
            fn_800832D8();
        }
    }
}
