#include <dolphin/types.h>

extern void *lbl_804E6F38;
extern u8 lbl_804E6F28[];
extern u8 lbl_805984E4[];
extern u32 lbl_8074E648;
extern char lbl_804704E8[];

extern int sprintf(char *, const char *, ...);
extern void *memcpy(void *, const void *, u32);
extern void DCFlushRange(void *, u32);

void *fn_800B6750(void *param1) {
    fn_800860D0();
    *(void **)((u8 *)param1 + 0x4) = &lbl_804E6F38;
    *(u8 *)((u8 *)param1 + 0x56) = 0;
    fn_8044E450((u8 *)param1 + 0x74, fn_800B67B4, fn_800B67C0, 4, 4);
    return param1;
}

void fn_800B67B4(void *param1) {
    *(u32 *)((u8 *)param1 + 0x0) = 0;
}

void *fn_800B67C0(void *param1, s32 param2) {
    void *saved = param1;
    if (param1 != 0 && param2 > 0) {
        fn_80440138();
    }
    return saved;
}

void *fn_800B6800(void *param1, s32 param2) {
    void *p1 = param1;
    s32 p2 = param2;
    if (param1 != 0) {
        fn_8044E548((u8 *)p1 + 0x74, fn_800B67C0, 4, 4);
        if (p1 != 0) {
        }
        if (p1 != 0) {
        }
        if (p1 != 0) {
            fn_80085EB4(p1, 0);
        }
    }
    if (p2 > 0) {
        fn_80440138(p1);
    }
    return p1;
}

s32 fn_800B6884(void) {
    return 0x1EFE0;
}

void fn_800B6890(void *param1, void *param2, void *param3) {
    void *tmp = param1;
    void *p3 = param2;
    void *p2 = tmp;
    return fn_800B69A4(lbl_805984E4, p2, p3);
}

s32 fn_800B68A8(void *param1) {
    void *p1 = param1;
    u32 val = lbl_8074E648;
    if (val == 0) {
        return 1;
    }
    {
        s32 result = fn_80086134(lbl_804704E8, val, 0);
        if (result == 0) {
            return 0;
        }
        {
            void *p30 = (u8 *)p1 + 0x74;
            void *p29 = (u8 *)p30 + 0x10;
            while (p30 != p29) {
                fn_800B6A24(p30, val);
                p30 = (u8 *)p30 + 0x4;
            }
            return 1;
        }
    }
}

void *fn_800B6940(s32 param1) {
    void *result = 0;
    if (param1 < 4) {
        u32 offset = (u32)param1 << 2;
        void *p = lbl_805984E4 + offset;
        result = (u8 *)p + 0x74;
    }
    return result;
}

void fn_800B6968(void *param1) {
    void *saved = param1;
    fn_8037420C(*(void **)((u8 *)param1 + 0x10), (u8 *)param1 + 0x58);
    *(u8 *)((u8 *)saved + 0x56) = 1;
}

void fn_800B69A4(void *param1, void *param2, s32 param3) {
    void *p1 = param1;
    void *p2 = param2;
    if (param3 < 2) {
        u32 tmp = 0;
        char buf[16];
        *(u32 *)&tmp = 0;
        sprintf(buf, (char *)lbl_804E6F28);
        fn_800863A0(p1, buf, &tmp);
        if (p2 != 0) {
            *(u32 *)((u8 *)p2 + 0x0) = tmp;
        }
    } else {
        return (void)0;
    }
}

s32 fn_800B6A24(void *param1, void *param2) {
    void *p1 = param1;
    void *p2 = param2;
    u32 val = *(u32 *)((u8 *)p1 + 0x0);
    if (val == 0) {
        void *vtable = *(void **)p2;
        void *(*fn)(void *, s32, s32) = *(void **)((u8 *)vtable + 0x14);
        void *result = fn(p2, 0x5380, 0x20);
        *(u32 *)((u8 *)p1 + 0x0) = (u32)result;
    }
    return 1;
}

void fn_800B6A7C(void *param1, void *param2, s32 param3) {
    void *p1 = param1;
    s32 len = 0x5380;
    if (param3 < 0x5380) {
        len = param3;
    }
    memcpy(*(void **)((u8 *)p1 + 0x0), param2, (u32)len);
    DCFlushRange(*(void **)((u8 *)p1 + 0x0), (u32)len);
}

void fn_800B6AD4(void *param1) {
    *(u8 *)((u8 *)param1 + 0x0) = 4;
}

void *fn_800B6AE0(void *param1, s32 param2) {
    void *saved = param1;
    if (param1 != 0 && param2 > 0) {
        fn_80440138();
    }
    return saved;
}

void fn_800B6B20(void *param1, u8 param2) {
    *(u8 *)((u8 *)param1 + 0x0) = param2;
}

s32 fn_800B6B28(void *param1, void *param2) {
    void *p1 = param1;
    void *p2 = param2;
    u8 idx = *(u8 *)((u8 *)param1 + 0x0);
    void *slot = fn_800B6940((s32)idx);
    void *saved_slot = slot;
    if (slot == 0) {
        return 0;
    }
    {
        u32 tmp = 0;
        s32 result = fn_800B6890(&tmp, p2, &tmp);
        s32 res2 = result;
        if (result == 0) {
            return 0;
        }
        {
            u32 tmp2 = *(u32 *)((u8 *)&tmp + 0x0);
            fn_800B6A7C(saved_slot, (void *)(u32)res2, (s32)tmp2);
        }
        fn_800B6BF0(p1);
        return 1;
    }
}

void *fn_800B6BB8(void *param1) {
    u8 idx = *(u8 *)((u8 *)param1 + 0x0);
    void *slot = fn_800B6940((s32)idx);
    if (slot != 0) {
        return *(void **)((u8 *)slot + 0x0);
    }
    return 0;
}

void fn_800B6BF0(void *param1) {
    void *ptr = fn_800B6BB8(param1);
    *(u32 *)((u8 *)&ptr + 0x0) = (u32)ptr;
    if (ptr == 0) {
        return;
    }
    {
        void *pptr = &ptr;
        fn_80234FE8(pptr);
        u32 tmp = *(u32 *)((u8 *)&ptr + 0x0);
        void *pptr2 = &ptr;
        void *pptr3 = &tmp;
        *(u32 *)((u8 *)&tmp) = tmp;
        fn_80234BB4(pptr2, pptr3);
    }
}

void *fn_800B6C38(void) {
    return fn_800B6884();
}

s32 fn_800B6C3C(void) {
    return fn_800B68A8(lbl_805984E4);
}
