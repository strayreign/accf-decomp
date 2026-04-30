#include <dolphin/types.h>

extern int sprintf(char *, const char *, ...);
extern void fn_80085EB4(void *, u32);
extern void fn_80086134(void *, const char *, void *, u32);
extern void fn_800887FC(void *, u32);
extern void fn_80162A80(void *);
extern void fn_80233910(void *, u32);
extern void fn_80234BB4(void *, void *);
extern void fn_80234FE8(void *);
extern void fn_80440138(void *);

extern u8 lbl_8046F498[];
extern char lbl_804E5B60[];
extern void *lbl_804E5BC0[];
extern u32 lbl_804E5CB8[];
extern u8 lbl_8058B898[];
extern u32 lbl_8058B900[];

void fn_800A7098(u16 *dst, u16 *src, u16 val);

void fn_800A7094(u16 *dst, u16 *src, u16 val)
{
    return fn_800A7098(dst, src, val);
}

void fn_800A7098(u16 *dst, u16 *src, u16 val)
{
    u16 tmp;
    tmp = *src;
    tmp = (u16)((tmp & 0xFFFC) | (val & 3));
    *dst = tmp;
}

void *fn_800A70AC(void)
{
    return (void *)lbl_8058B898;
}

void fn_800A70B8(void *param1, void *param2)
{
    char buf[0x44];
    void *tmp;
    fn_80162A80(param1);
    tmp = param1;
    sprintf(buf, lbl_804E5B60, tmp);
    fn_80086134(param1, buf, param2, 0);
}

void fn_800A711C(void *param1)
{
    u32 local_10;
    u32 local_08;
    u32 local_0C;
    u8 *base;
    u32 *cbdata;
    u32 *names;
    s32 i;
    u32 idx;
    u32 result_size;
    u32 result_ptr;
    u32 neg_rs;
    u32 or_rs;
    s32 sign_rs;
    u32 and_rs;

    local_10 = *(u32 *)((u8 *)param1 + 0x10);
    fn_80234FE8((void *)&local_10);
    local_08 = local_10;
    fn_80234BB4((void *)&local_10, (void *)&local_08);

    local_0C = *(u32 *)((u8 *)lbl_8058B900 + 0x10);

    base = lbl_8046F498;
    cbdata = lbl_804E5CB8;
    names = (u32 *)lbl_804E5BC0;

    i = 0;
    do {
        idx = (u32)(*base) << 2;
        fn_80233910((void *)&local_0C, *(u32 *)((u8 *)names + idx));

        result_size = *(u32 *)((u8 *)&local_0C + 0x10);
        result_ptr = (u32)(u32 *)&local_0C + result_size;

        neg_rs = (u32)(-(s32)result_size);
        or_rs = neg_rs | result_size;
        sign_rs = (s32)or_rs >> 31;
        and_rs = result_ptr & (u32)sign_rs;

        fn_800887FC((void *)(*cbdata), and_rs);

        i += 1;
        cbdata += 1;
        base += 1;
    } while (i < 0xf);
}

void *fn_800A71F4(void *param1, s32 param2)
{
    void *saved1;
    s32 saved2;

    saved1 = param1;
    saved2 = param2;

    if (param1 == 0) {
        goto done;
    }
    if (param1 == 0) {
        goto check_p2;
    }
    fn_80085EB4(param1, 0);
check_p2:
    if (saved2 > 0) {
        fn_80440138(saved1);
    }
done:
    return saved1;
}
