#ifndef EGG_COLOR_FADER_H
#define EGG_COLOR_FADER_H

#include <dolphin/types.h>

#ifdef __cplusplus
extern "C" {
#endif

/* EGG::ColorFader fade state */
typedef enum EFaderStatus {
    FADER_STATUS_IDLE    = 0,
    FADER_STATUS_FADE_IN = 1,
    FADER_STATUS_FADE_OUT = 2,
    FADER_STATUS_OPAQUE  = 3,
} EFaderStatus;

/*
 * EGG::ColorFader  (sizeof = 0x38)
 *
 * Manages full-screen colour fades.  Fields from +0x24 onward are inferred
 * from fn_804526B4; names are provisional until further analysis.
 */
typedef struct EGG_ColorFader {
    void         *vtable;         /* +0x00  vptr                          */
    EFaderStatus  mStatus;        /* +0x04  current fade state (4 bytes)  */
    u8            mFlags;         /* +0x08                                */
    u8            _pad09;         /* +0x09  alignment padding             */
    u16           mFrame;         /* +0x0A  total frames for the fade     */
    u16           mFrameTimer;    /* +0x0C  elapsed frame counter         */
    u8            mColor_r;       /* +0x0E  target colour -- red           */
    u8            mColor_g;       /* +0x0F  target colour -- green         */
    u8            mColor_b;       /* +0x10  target colour -- blue          */
    u8            mColor_a;       /* +0x11  target colour -- alpha         */
    u8            _pad12[2];      /* +0x12  alignment padding             */
    f32           mRect_left;     /* +0x14  screen rect left              */
    f32           mRect_top;      /* +0x18  screen rect top               */
    f32           mRect_right;    /* +0x1C  screen rect right             */
    f32           mRect_bottom;   /* +0x20  screen rect bottom            */
    u32           _unk_0x24;      /* +0x24  (written from mRect_right)    */
    s32           _unk_0x28;      /* +0x28  (computed delta value)        */
    u32           _unk_0x2C;      /* +0x2C  (mask / divisor for delta)    */
    u8            _pad30[4];      /* +0x30  unused / alignment            */
    u32           _unk_0x34;      /* +0x34  (stores mRect_top bits)       */
} EGG_ColorFader; /* sizeof = 0x38 */

#ifdef __cplusplus
}
#endif

#endif /* EGG_COLOR_FADER_H */
