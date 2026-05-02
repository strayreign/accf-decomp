#include <dolphin/types.h>

extern void fn_80085EB4(void* param_1, u32 param_2);
extern s32 fn_80085EF4(void* param_1, void* param_2, u32 param_3, u32 param_4, void* param_5);
extern s32 fn_8022D800(void* param_1, u32 param_2);
extern void fn_8022DC84(void* param_1, void* param_2, void* param_3, u32 param_4, void* param_5);
extern s32 fn_80234BB4(void* param_1, void* param_2);
extern s32 fn_80234FE8(void* param_1);
extern void* fn_8043FB5C(void* param_1, u32 param_2);
extern void fn_80440138(void* param_1);
extern void fn_8044086C(void* param_1, u32 param_2);

extern u8 lbl_804E5D68[0x24];
extern u32 lbl_8058B958[5];
extern u8 lbl_8058B97C[0x64];
extern u32* lbl_8074E658;
extern u32* lbl_8074E65C;
extern u32 lbl_80753C78;

void* fn_800A72CC(void* param_1, s32 param_2)
{
    if (param_1 != NULL) {
        fn_80085EB4(param_1, 0);
        if (param_2 > 0) {
            fn_80440138(param_1);
        }
    }
    return param_1;
}

void fn_800A7328(void* param_1)
{
    u32 local_4;
    u32 local_8;

    local_4 = *(u32*)((u8*)param_1 + 0x10);
    local_8 = local_4;
    fn_80234FE8(&local_8);
    fn_80234BB4(&local_4, &local_8);
}

void fn_800A7368(void)
{
    *(u32*)lbl_8058B958 = 0;
    *(u32*)((u8*)lbl_8058B958 + 0x04) = 0;
    *(u32*)((u8*)lbl_8058B958 + 0x08) = 0;
    *(u32*)((u8*)lbl_8058B958 + 0x0C) = 0;
    *(u32*)((u8*)lbl_8058B958 + 0x10) = 0;
}

s32 fn_800A738C(void* param_1)
{
    u32 i;
    u32 val;

    if (param_1 == NULL) {
        val = *(u32*)lbl_8058B958;
        if (val == 0) {
            *(u32*)lbl_8058B958 = 0;
            return 1;
        }
        return 0;
    }

    val = *(u32*)lbl_8058B958;
    if (val == (u32)param_1) {
        return 1;
    }
    val = *(u32*)((u8*)lbl_8058B958 + 0x04);
    if (val == (u32)param_1) {
        return 1;
    }
    val = *(u32*)((u8*)lbl_8058B958 + 0x08);
    if (val == (u32)param_1) {
        return 1;
    }
    val = *(u32*)((u8*)lbl_8058B958 + 0x0C);
    if (val == (u32)param_1) {
        return 1;
    }
    val = *(u32*)((u8*)lbl_8058B958 + 0x10);
    if (val == (u32)param_1) {
        return 1;
    }

    for (i = 0; i < 5; i++) {
        val = *(u32*)((u8*)lbl_8058B958 + (i << 2));
        if (val == 0) {
            *(u32*)((u8*)lbl_8058B958 + (i << 2)) = (u32)param_1;
            return 1;
        }
    }
    return 0;
}

s32 fn_800A7448(void* param_1)
{
    u32 i;
    u32 val;

    if (param_1 == NULL) {
        return 0;
    }

    for (i = 0; i < 5; i++) {
        val = *(u32*)((u8*)lbl_8058B958 + (i << 2));
        if (val == (u32)param_1) {
            *(u32*)((u8*)lbl_8058B958 + (i << 2)) = 0;
            return 1;
        }
    }
    return 0;
}

s32 fn_800A74A0(void)
{
    s32 count;
    u32 val;

    count = 0;

    val = *(u32*)lbl_8058B958;
    if (val != 0) count = 1;

    val = *(u32*)((u8*)lbl_8058B958 + 0x04);
    if (val != 0) count = count + 1;

    val = *(u32*)((u8*)lbl_8058B958 + 0x08);
    if (val != 0) count = count + 1;

    val = *(u32*)((u8*)lbl_8058B958 + 0x0C);
    if (val != 0) count = count + 1;

    val = *(u32*)((u8*)lbl_8058B958 + 0x10);
    if (val != 0) count = count + 1;

    return count;
}

void* fn_800A74FC(u32 param_1)
{
    u32 i;
    u32 count;
    u32 val;

    count = 0;

    for (i = 0; i < 5; i++) {
        val = *(u32*)((u8*)lbl_8058B958 + (i << 2));
        if (val != 0) {
            if (count == param_1) {
                return (void*)*(u32*)((u8*)lbl_8058B958 + (i << 2));
            }
            count = count + 1;
        }
    }
    return NULL;
}

s32 fn_800A7554(void* param_1)
{
    u32 i;
    u32 val;
    void* obj;
    u32* vtable;
    s32 (*fn)(void*);

    for (i = 0; i < 5; i++) {
        val = *(u32*)((u8*)lbl_8058B958 + (i << 2));
        if (val == 0 || val == (u32)param_1) {
            continue;
        }
        obj = (void*)val;
        vtable = *(u32**)((u8*)obj + 0x60);
        fn = (s32 (*)(void*))(*(u32*)((u8*)vtable + 0x50));
        return fn(obj);
    }
    return -1;
}

s32 fn_800A75B0(void)
{
    u32* ptr;
    u32 field_50;
    void* heap;
    s32 result;
    void* alloc_ptr;
    void* obj;

    ptr = (u32*)lbl_8058B97C;
    field_50 = *(u32*)((u8*)ptr + 0x50);

    if (field_50 == 0) {
        heap = *lbl_8074E658;
        result = fn_80085EF4((void*)((u8*)ptr + 0x3C), (void*)&lbl_804E5D68[0], 0, 0, heap);
        if (result == 0) {
            return 0;
        }

        alloc_ptr = fn_8022D800((void*)&lbl_80753C78, 0x32);
        obj = fn_8043FB5C(alloc_ptr, 0x20);
        *(u32*)((u8*)ptr + 0x50) = (u32)obj;
        fn_8022DC84(ptr, obj, alloc_ptr, 0, (void*)&lbl_80753C78);
        fn_8044086C(heap, 3);
    }

    return 1;
}

u32 fn_800A7678(void)
{
    return 0x28;
}
</artifact>
