#include <dolphin/types.h>

// Returns the sign bit of a double as a mask: 0x80000000 if negative, 0 if non-negative.
// Used by the printf float formatting routines.
s32 fn_804535F4(f64 x) {
    union {
        f64 d;
        s32 i[2];
    } u;
    u.d = x;
    return u.i[0] & ~0x7FFFFFFF;
}
