
// Compiler hint (level 1): Force unsigned char
// Flags: -unsigned-char
#include <dolphin/types.h>

// ---------------------------------------------------------------------------
// DVD range / position clamping helpers
//
// DVDRange tracks a [0, end] window.  fn_8022AF78 advances pos by a signed
// delta clamped to that window.  fn_8022AFDC additionally lets callers reset
// pos to 0 (mode=0) or to end (mode=2) before applying the delta.
// ---------------------------------------------------------------------------

struct DVDRange {
    u32 end;  // upper bound (inclusive)
    u32 pos;  // current position, always in [0, end]
};

// Advance range->pos by delta, clamping to [0, range->end].
// Returns the updated pos.
s32 fn_8022AF78(DVDRange* range, s32 delta) {
    if (delta == 0)
        return (s32)range->pos;

    s32 newPos = (s32)range->pos + delta;

    if (newPos > (s32)range->end)
        newPos = (s32)range->end;
    else if (newPos < 0)
        newPos = 0;

    range->pos = (u32)newPos;
    return (s32)range->pos;
}

// Optionally reset pos (mode 0 = zero, mode 2 = end), then advance by delta.
void fn_8022AFDC(DVDRange* range, s32 delta, s32 mode) {
    if (mode == 0)
        range->pos = 0;
    else if (mode == 2)
        range->pos = range->end;
    // mode 1 or >= 3: leave pos unchanged

    if (delta == 0)
        return;

    s32 newPos = (s32)range->pos + delta;

    if (newPos > (s32)range->end)
        newPos = (s32)range->end;
    else if (newPos < 0)
        newPos = 0;

    range->pos = (u32)newPos;
}
