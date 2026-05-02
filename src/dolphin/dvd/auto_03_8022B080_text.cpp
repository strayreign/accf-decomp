#include <dolphin/types.h>

// ---------------------------------------------------------------------------
// DVD Buffered-Read Object
//
// Wraps a lower-level DVD async-read sub-object (at +0x28) with:
//   - a clamped byte-range tracker (DVDRange at +0x14)
//   - vtable / virtual destructor support
//   - two async callbacks (fn_8022B080 / fn_8022B0AC)
//
// Struct layout (0x6F bytes minimum):
//   +0x00  void**  vtable          (lbl_805214A8)
//   +0x04  u8      active          (bool, set 1 on successful init)
//   +0x08  u32     result          (last async result written by callback)
//   +0x0c  void*   userCallback
//   +0x10  void*   userCallbackArg
//   +0x14  u32     rangeEnd        (DVDRange.end)
//   +0x18  u32     rangePos        (DVDRange.pos)
//   +0x1c  void*   field_1c
//   +0x20  void*   field_20
//   +0x24  u8      field_24
//   +0x28  u8[60]  dvdSubObj       (passed to fn_8039A5F8 / DVDReadAsync etc.)
//   +0x64  this*   selfPtr
//   +0x68  u32     field_68        (init to 2)
//   +0x6c  u8      inUse           (DVDReadAsync in-flight flag)
//   +0x6d  u8      field_6d
//   +0x6e  u8      field_6e
// ---------------------------------------------------------------------------

struct DVDRange {
    u32 end;
    u32 pos;
};

struct DVDBufRead {
    void**       vtable;
    s8           active;
    u8           pad05[3];
    u32          result;
    void*        userCallback;
    void*        userCallbackArg;
    DVDRange     range;          // +0x14
    void*        field_1c;
    void*        field_20;
    u8           field_24;
    u8           pad25[3];
    u8           dvdSubObj[0x3c]; // +0x28 .. +0x63
    DVDBufRead*  selfPtr;         // +0x64
    u32          field_68;        // +0x68  init = 2
    u8           inUse;           // +0x6c
    s8           field_6d;
    s8           field_6e;
};

// ---------------------------------------------------------------------------
// External references
// ---------------------------------------------------------------------------

// vtable for DVDBufRead
extern "C" void* lbl_805214A8;

// SDA globals
extern "C" u32 lbl_8074F0E8;

// Range helpers (same TU as auto_03_8022AF78_text)
extern "C" s32  fn_8022AF78(DVDRange* range, s32 delta);
extern "C" void fn_8022AFDC(DVDRange* range, s32 delta, s32 mode);

// Sub-object operations
extern "C" void* fn_8039A5F8(void* param1, void* subObj);
extern "C" void  fn_8039A90C(void* subObj);
extern "C" s32   fn_8039ABD8(void* subObj, s32 offset, s32 size, u32 base, u32 handle);
extern "C" s32   fn_8039EFF0(void* subObj, void* callback, void* arg);
extern "C" void  fn_8039F354(void* subObj);

// Allocator / free
extern "C" void fn_80440138(void* ptr);

// DVD async read
extern "C" s32 DVDReadAsync(void* fileInfo, void* buf, s32 length, s32 offset,
                             void* callback, u32 handle);

// ---------------------------------------------------------------------------
// Inline helper: align value up to the nearest 32-byte boundary
// ---------------------------------------------------------------------------
static inline u32 roundup32(u32 x) { return (x + 0x1fu) & ~0x1fu; }

// ---------------------------------------------------------------------------
// fn_8022B080  -  primary async callback
//   r4 points to the DVDBufRead whose sub-object triggered the callback.
//   Clears inUse, stores the result, and fires the user callback if set.
// ---------------------------------------------------------------------------
void fn_8022B080(s32 result, void* subObjArg) {
    // subObjArg is &this->dvdSubObj; selfPtr lives at dvdSubObj+0x3C (= this+0x64)
    DVDBufRead* obj = *(DVDBufRead**)((u8*)subObjArg + 0x3c);

    obj->inUse  = 0;
    obj->result = (u32)result;

    void (*cb)(s32, DVDBufRead*, void*) = (void (*)(s32, DVDBufRead*, void*))obj->userCallback;
    if (cb != 0) {
        void* arg = obj->userCallbackArg;
        cb(result, obj, arg);
    }
}

// ---------------------------------------------------------------------------
// fn_8022B0AC  -  secondary async callback (used by fn_8022B5D0 / fn_8039EFF0)
// ---------------------------------------------------------------------------
void fn_8022B0AC(s32 result, void* subObjArg) {
    DVDBufRead* obj = *(DVDBufRead**)((u8*)subObjArg + 0x3c);

    obj->field_24 = 0;

    void (*cb)(s32, DVDBufRead*, void*) = (void (*)(s32, DVDBufRead*, void*))obj->field_1c;
    if (cb != 0) {
        void* arg = obj->field_20;
        cb(result, obj, arg);
    }
}

// ---------------------------------------------------------------------------
// fn_8022B0D4  -  single-arg constructor
//   param1 is passed straight to fn_8039A5F8 as its first argument.
// ---------------------------------------------------------------------------
DVDBufRead* fn_8022B0D4(DVDBufRead* self, void* param1) {
    self->inUse      = 0;
    self->vtable     = (void**)&lbl_805214A8;
    self->range.end  = 0;
    self->range.pos  = 0;
    self->field_6d   = 0;
    self->field_6e   = 0;
    self->active     = 0;
    self->field_68   = 2;
    self->userCallback    = 0;
    self->userCallbackArg = 0;
    self->result     = 0;
    self->field_1c   = 0;
    self->field_24   = 0;
    self->field_20   = 0;
    self->selfPtr    = self;

    void* subObjResult = fn_8039A5F8(param1, self->dvdSubObj);
    if (subObjResult != 0) {
        self->range.end = ((u32*)self->dvdSubObj)[0x34 / 4]; // sub-obj word at +0x34
        fn_8022AFDC(&self->range, 0, 0);
        self->field_6d = 1;
        self->field_6e = 1;
        self->active   = 1;
    }

    return self;
}

// ---------------------------------------------------------------------------
// fn_8022B190  -  three-arg constructor  (copy from an existing sub-object)
// ---------------------------------------------------------------------------
void fn_8022B190(DVDBufRead* self, void* srcSubObj, u8 flag) {
    self->inUse      = 0;
    self->vtable     = (void**)&lbl_805214A8;
    self->range.end  = 0;
    self->range.pos  = 0;
    self->field_6d   = 0;
    self->field_6e   = 0;
    self->active     = 0;
    self->field_68   = 2;
    self->userCallback    = 0;
    self->userCallbackArg = 0;
    self->result     = 0;
    self->field_1c   = 0;
    self->field_24   = 0;
    self->field_20   = 0;
    self->selfPtr    = self;

    // Copy 15 words (0x3c bytes) from srcSubObj[0..0x38] into dvdSubObj[0..0x3c]
    u32* src  = (u32*)srcSubObj;
    u32* dst  = (u32*)self->dvdSubObj;
    dst[0]  = src[0x00/4];
    dst[1]  = src[0x04/4];
    dst[2]  = src[0x08/4];
    dst[3]  = src[0x0c/4];
    dst[4]  = src[0x10/4];
    dst[5]  = src[0x14/4];
    dst[6]  = src[0x18/4];
    dst[7]  = src[0x1c/4];
    dst[8]  = src[0x20/4];
    dst[9]  = src[0x24/4];
    dst[10] = src[0x28/4];
    dst[11] = src[0x2c/4];
    dst[12] = src[0x30/4];
    dst[13] = src[0x34/4];   // also set as rangeEnd below
    dst[14] = src[0x38/4];

    self->range.end = src[0x34/4];
    fn_8022AFDC(&self->range, 0, 0);

    self->field_6d = 0;
    self->field_6e = flag;
    self->active   = 1;
}

// ---------------------------------------------------------------------------
// fn_8022B2C0  -  destructor  (param2 > 0 -> free the allocation)
// ---------------------------------------------------------------------------
DVDBufRead* fn_8022B2C0(DVDBufRead* self, s32 freeFlag) {
    if (self == 0)
        return self;

    // Restore vtable (CW destructor pattern)
    self->vtable = (void**)&lbl_805214A8;

    if (self->field_6d != 0) {
        // Call virtual destructor slot (vtable[4])
        void (*vdtor)(DVDBufRead*) =
            (void (*)(DVDBufRead*))((void**)self->vtable)[4];
        vdtor(self);
    }

    if (freeFlag > 0)
        fn_80440138(self);

    return self;
}

// ---------------------------------------------------------------------------
// fn_8022B338  -  cancel / stop pending async read
// ---------------------------------------------------------------------------
void fn_8022B338(DVDBufRead* self) {
    if (self->field_6e == 0 || self->active == 0)
        return;

    fn_8039A90C(self->dvdSubObj);
    self->active = 0;
}

// ---------------------------------------------------------------------------
// fn_8022B388  -  buffered synchronous-style read
//   Clamps the requested size to what remains in the buffer, calls the
//   lower-level read, then advances rangePos by the number of bytes read.
// ---------------------------------------------------------------------------
s32 fn_8022B388(DVDBufRead* self, s32 offset, s32 size) {
    u32 base    = self->range.pos;    // field_0x18
    u32 limit   = self->range.end;    // field_0x14
    u32 clampedSize = (u32)size;

    // If roundup32(base + size) > roundup32(limit), shrink to roundup32(limit - base)
    if (roundup32(base + (u32)size) > roundup32(limit))
        clampedSize = roundup32(limit - base);

    u32 handle = self->field_68;
    s32 result = fn_8039ABD8(self->dvdSubObj, offset, (s32)clampedSize, base, handle);

    if (result > 0)
        fn_8022AF78(&self->range, result);

    return result;
}

// ---------------------------------------------------------------------------
// fn_8022B410  -  full async DVDReadAsync wrapper with buffer-size clamping
// ---------------------------------------------------------------------------
s32 fn_8022B410(DVDBufRead* self, void* buf, s32 offset, void* callback, void* callbackArg) {
    u32 base  = self->range.pos;
    u32 limit = self->range.end;
    u32 clampedOffset = (u32)offset;

    if (roundup32(base + (u32)offset) > roundup32(limit))
        clampedOffset = roundup32(limit - base);

    self->userCallback    = callback;
    self->userCallbackArg = callbackArg;

    u32 base2  = self->range.pos;
    u32 limit2 = self->range.end;
    u32 newEnd = base2 + clampedOffset;
    u32 clampedSize = clampedOffset;

    if (roundup32(newEnd) > roundup32(limit2))
        clampedSize = roundup32(limit2 - base2);

    self->inUse = 1;

    u32 handle = self->field_68;
    s32 ret = DVDReadAsync(self->dvdSubObj, buf, (s32)clampedSize,
                           (s32)base2, (void*)fn_8022B080, handle);

    // neg/or/srwi idiom: success = (ret != 0)
    s32 success = (s32)((u32)((-ret) | ret) >> 31);
    if (success) {
        fn_8022AF78(&self->range, (s32)clampedOffset);
    } else {
        self->inUse = 0;
    }

    return success;
}

// ---------------------------------------------------------------------------
// fn_8022B504  -  compute clamped read size, tail-call fn_8039ABD8
// ---------------------------------------------------------------------------
s32 fn_8022B504(DVDBufRead* self, s32 offset, s32 size) {
    u32 base  = self->range.pos;
    u32 limit = self->range.end;
    u32 clampedSize = (u32)size;

    if (roundup32(base + (u32)size) > roundup32(limit))
        clampedSize = roundup32(limit - base);

    u32 handle = self->field_68;
    return fn_8039ABD8(self->dvdSubObj, offset, (s32)clampedSize, base, handle);
}

// ---------------------------------------------------------------------------
// fn_8022B540  -  async DVDReadAsync with no position update
// ---------------------------------------------------------------------------
s32 fn_8022B540(DVDBufRead* self, void* buf, s32 size, void* callback, void* callbackArg) {
    u32 base  = self->range.pos;
    u32 limit = self->range.end;
    u32 clampedSize = (u32)size;

    if (roundup32(base + (u32)size) > roundup32(limit))
        clampedSize = roundup32(limit - base);

    self->userCallback    = callback;
    self->userCallbackArg = callbackArg;
    self->inUse = 1;

    u32 handle = self->field_68;
    s32 ret = DVDReadAsync(self->dvdSubObj, buf, (s32)clampedSize,
                           (s32)base, (void*)fn_8022B080, handle);

    s32 success = (s32)((u32)((-ret) | ret) >> 31);
    return success;
}

// ---------------------------------------------------------------------------
// fn_8022B5C0  -  thunk: fn_8022AFDC on self->range
// ---------------------------------------------------------------------------
void fn_8022B5C0(DVDBufRead* self, s32 delta, s32 mode) {
    fn_8022AFDC(&self->range, delta, mode);
}

// ---------------------------------------------------------------------------
// fn_8022B5C8  -  thunk: fn_8039F354 on sub-object
// ---------------------------------------------------------------------------
void fn_8022B5C8(DVDBufRead* self) {
    fn_8039F354(self->dvdSubObj);
}

// ---------------------------------------------------------------------------
// fn_8022B5D0  -  open secondary channel
// ---------------------------------------------------------------------------
s32 fn_8022B5D0(DVDBufRead* self, void* cbFunc, void* cbArg) {
    self->field_1c = cbFunc;
    self->field_20 = cbArg;

    s32 result = fn_8039EFF0(self->dvdSubObj, (void*)fn_8022B0AC, cbArg);
    if (result != 0)
        self->field_24 = 1;

    return (s32)((u32)((-result) | result) >> 31);
}

// ---------------------------------------------------------------------------
// Trivial getters / constant returns
// ---------------------------------------------------------------------------
s32  fn_8022B62C(void) { return 0x20; }   // sector size (32)
s32  fn_8022B634(void) { return 0x20; }   // sector size (32)
s32  fn_8022B63C(void) { return 0x4;  }   // alignment
s32  fn_8022B644(void) { return 0x1;  }
s32  fn_8022B64C(void) { return 0x0;  }
s32  fn_8022B654(void) { return 0x1;  }
s32  fn_8022B65C(void) { return 0x1;  }
s32  fn_8022B664(void) { return 0x1;  }

u32  fn_8022B66C(DVDBufRead* self) { return self->range.end; }
u32  fn_8022B674(DVDBufRead* self) { return self->range.pos; }
u8   fn_8022B67C(DVDBufRead* self) { return self->inUse;     }

u32* fn_8022B684(void) { return &lbl_8074F0E8; }
