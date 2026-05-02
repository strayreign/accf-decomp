#include <dolphin/os/OSCache.h>

#define CACHE_LINE_SIZE 32

// Enable data cache (sets DCE bit in HID0)
void DCEnable(void) {
    register u32 hid0;
    // sync before enabling cache
    __asm {
        sync
        mfhid0  hid0
        ori     hid0, hid0, 0x4000   // set DCE (bit 14)
        mthid0  hid0
        isync
    }
}

// Invalidate (discard without writeback) a range of data cache lines
void DCInvalidateRange(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __dcbi(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
}

// Flush (writeback + invalidate) a range and sync
void DCFlushRange(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __dcbf(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
    __sync();
}

// Store (writeback, keep valid) a range and sync
void DCStoreRange(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __dcbst(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
    __sync();
}

// Flush (writeback + invalidate) a range without sync
void DCFlushRangeNoSync(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __dcbf(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
}

// Store (writeback, keep valid) a range without sync
void DCStoreRangeNoSync(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __dcbst(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
}

// Zero out a range via dcbz (cache-line-zero, allocating cache lines)
void DCZeroRange(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __dcbz(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
}

// Invalidate a range of instruction cache lines, then sync
void ICInvalidateRange(void* addr, u32 nBytes) {
    register u32 base = (u32)addr;
    register u32 count;

    if (nBytes == 0) {
        return;
    }
    count = (nBytes + (base & (CACHE_LINE_SIZE - 1)) + (CACHE_LINE_SIZE - 1)) >> 5;
    do {
        __icbi(0, base);
        base += CACHE_LINE_SIZE;
    } while (--count != 0);
    __sync();
    __isync();
}

// Flash-invalidate the entire instruction cache (sets ICFI bit in HID0)
void ICFlashInvalidate(void) {
    register u32 hid0;
    __asm {
        mfhid0  hid0
        ori     hid0, hid0, 0x800    // set ICFI (bit 20)
        mthid0  hid0
    }
}

// Enable instruction cache (sets ICE bit in HID0)
void ICEnable(void) {
    register u32 hid0;
    __asm {
        isync
        mfhid0  hid0
        ori     hid0, hid0, 0x8000   // set ICE (bit 16)
        mthid0  hid0
    }
}
