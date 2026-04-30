#include <dolphin/os/IPC.h>
#include <dolphin/os/OSInterrupt.h>

// ---------------------------------------------------------------------------
// IOS Heap Implementation
//
// A simple first-fit allocator that lives entirely within a caller-supplied
// buffer.  Up to IOS_HEAP_MAX_HEAPS (8) heaps can be registered.
//
// Block header layout (16 bytes, cache-line-aligned):
//   [+0x00] u32  magic   -- IOS_HEAP_MAGIC_FREE / ALLOC / PAD
//   [+0x04] u32  size    -- usable bytes that follow this header
//   [+0x08] void* prev   -- previous free block (NULL if head)
//   [+0x0C] void* next   -- next free block    (NULL if tail)
// ---------------------------------------------------------------------------

#define IOS_HEAP_MAX_HEAPS   8
#define IOS_HEAP_HDR_SIZE   16      // sizeof block header
#define IOS_HEAP_MIN_ALIGN  0x20    // minimum allocation alignment

#define IOS_HEAP_MAGIC_FREE  0xBabe0000
#define IOS_HEAP_MAGIC_ALLOC 0xBabe0001
#define IOS_HEAP_MAGIC_PAD   0xBabe0002

#define IOS_OK       0
#define IOS_EINVAL  -4
#define IOS_EMAX    -5

// Per-heap descriptor
typedef struct IPCHeapDesc {
    void*  base;   // start of the memory region  [8065F0A0 + idx*0x10 + 0]
    s32    size;   // total bytes in the region    [8065F0A0 + idx*0x10 + 8]
    void*  align;  // (padding helper pointer)     [8065F0A0 + idx*0x10 + C]
    void*  free;   // head of the free list        [8065F0AC + idx*0x10]
} IPCHeapDesc;

static IPCHeapDesc sIPCHeaps[IOS_HEAP_MAX_HEAPS];   // [8065F0A0]

// ---------------------------------------------------------------------------
// iosCreateHeap
//   Registers a memory region as an IOS heap and returns its handle (0-7).
//   The region must be 32-byte aligned.  Returns a negative error code on
//   failure.
// ---------------------------------------------------------------------------
s32 iosCreateHeap(void* addr, s32 size) {
    s32  handle;
    void* blk;

    handle = IOS_EINVAL;

    OSDisableInterrupts();

    if (((u32)addr & (IOS_HEAP_MIN_ALIGN - 1)) == 0) {
        // Find a free heap slot
        handle = 0;
        while (handle < IOS_HEAP_MAX_HEAPS && sIPCHeaps[handle].base != NULL) {
            handle++;
        }
        if (handle == IOS_HEAP_MAX_HEAPS) {
            handle = IOS_EMAX;
        } else {
            sIPCHeaps[handle].base  = addr;
            sIPCHeaps[handle].size  = size;
            sIPCHeaps[handle].align = addr;
            sIPCHeaps[handle].free  = addr;

            // Initialise the first (and only) free block to cover the whole region
            blk = addr;
            *(u32*)((u8*)blk + 0x00) = IOS_HEAP_MAGIC_FREE;
            *(s32*)((u8*)blk + 0x04) = size - IOS_HEAP_HDR_SIZE;
            *(void**)((u8*)blk + 0x08) = NULL;
            *(void**)((u8*)blk + 0x0C) = NULL;
        }
    }

    OSRestoreInterrupts();
    return handle;
}

// ---------------------------------------------------------------------------
// iosAllocAligned  (thunk -- calls the internal allocator)
//   Allocates 'size' bytes from heap 'heap' with 'align' byte alignment.
//   Returns a pointer to usable memory or NULL on failure.
// ---------------------------------------------------------------------------
void* iosAllocAligned(s32 heap, u32 size, u32 align) {
    IPCHeapDesc* h;
    void*  blk;
    void*  best;
    u32    roundSize;
    u32    pad;
    void*  ret;

    ret = NULL;

    OSDisableInterrupts();

    if (size != 0 && align != 0 && (align & (align - 1)) == 0) {
        if (align < IOS_HEAP_MIN_ALIGN) {
            align = IOS_HEAP_MIN_ALIGN;
        }

        roundSize = (size + (IOS_HEAP_MIN_ALIGN - 1)) & ~(IOS_HEAP_MIN_ALIGN - 1);

        if (heap < 0 || heap >= IOS_HEAP_MAX_HEAPS || sIPCHeaps[heap].base == NULL) {
            ret = NULL;
        } else {
            h   = &sIPCHeaps[heap];
            best = NULL;

            // First-fit search (prefer exact-size match)
            for (blk = h->free; blk != NULL; blk = *(void**)((u8*)blk + 0x0C)) {
                u32 blkSize = *(u32*)((u8*)blk + 0x04);
                pad = (align - 1) & (align - ((u32)((u8*)blk + 4) & (align - 1)));

                if (blkSize == roundSize && pad == 0) {
                    best = blk;
                    break;
                }
                if (roundSize + pad <= blkSize) {
                    if (best == NULL || blkSize < *(u32*)((u8*)best + 0x04)) {
                        best = blk;
                    }
                }
            }

            if (best != NULL) {
                pad = (align - 1) & (align - ((u32)((u8*)best + 4) & (align - 1)));

                // Split the block if there is leftover space
                if (roundSize + pad + IOS_HEAP_HDR_SIZE < *(u32*)((u8*)best + 0x04)) {
                    void* tail = (void*)((u8*)best + pad + roundSize + IOS_HEAP_HDR_SIZE);
                    *(u32*)((u8*)tail + 0x00) = IOS_HEAP_MAGIC_FREE;
                    *(u32*)((u8*)tail + 0x04) = (*(u32*)((u8*)best + 0x04) - roundSize - pad) - IOS_HEAP_HDR_SIZE;
                    // Splice tail into free list in place of best
                    void* nextFree = *(void**)((u8*)best + 0x0C);
                    *(void**)((u8*)tail + 0x0C) = nextFree;
                    if (nextFree) *(void**)((u8*)nextFree + 0x08) = tail;
                    *(void**)((u8*)best + 0x0C) = tail;
                    *(u32*)((u8*)best + 0x04) = roundSize + pad;
                }

                // Mark the block as allocated and remove from free list
                *(u32*)((u8*)best + 0x00) = IOS_HEAP_MAGIC_ALLOC;

                void* prevFree = *(void**)((u8*)best + 0x08);
                void* nextFree = *(void**)((u8*)best + 0x0C);
                if (prevFree == NULL) {
                    h->free = nextFree;
                } else {
                    *(void**)((u8*)prevFree + 0x0C) = nextFree;
                }
                if (nextFree) {
                    *(void**)((u8*)nextFree + 0x08) = prevFree;
                }

                // Return usable pointer (after header, respecting alignment pad)
                void* aligned = (void*)((u8*)best + pad);
                if (pad != 0) {
                    *(u32*)((u8*)aligned + 0x00) = IOS_HEAP_MAGIC_PAD;
                    *(void**)((u8*)aligned + 0x08) = best;
                }
                *(void**)((u8*)best + 0x08) = NULL;
                *(void**)((u8*)best + 0x0C) = NULL;

                ret = (void*)((u8*)aligned + 4);
            }
        }
    }

    OSRestoreInterrupts();
    return ret;
}

// ---------------------------------------------------------------------------
// iosFree
//   Returns a previously allocated block back to its heap.
//   Merges adjacent free blocks to prevent fragmentation.
//   Returns 0 on success or a negative error code.
// ---------------------------------------------------------------------------
s32 iosFree(s32 heap, void* ptr) {
    IPCHeapDesc* h;
    void*  hdr;
    void*  prev;
    void*  ins;
    void*  nxt;
    s32    result;

    result = IOS_EINVAL;

    OSDisableInterrupts();

    if (ptr != NULL) {
        if (heap < 0 || heap >= IOS_HEAP_MAX_HEAPS) {
            result = IOS_EINVAL;
        } else {
            h = &sIPCHeaps[heap];
            if (h->base == NULL) {
                goto done;
            }

            void* base = h->base;
            s32   sz   = h->size;

            if ((u32)ptr < (u32)base + IOS_HEAP_HDR_SIZE ||
                (u32)ptr > (u32)base + sz) {
                goto done;
            }

            // Resolve padding header if applicable
            hdr = (void*)((u8*)ptr - IOS_HEAP_HDR_SIZE);
            if (*(u32*)hdr == IOS_HEAP_MAGIC_PAD) {
                hdr = *(void**)((u8*)hdr + 8);
            }

            if (*(u32*)hdr != IOS_HEAP_MAGIC_ALLOC) {
                goto done;
            }

            // Mark free
            *(u32*)hdr = IOS_HEAP_MAGIC_FREE;

            // Insert into free list in address order
            prev = NULL;
            ins  = h->free;
            while (ins != NULL && ins <= hdr) {
                prev = ins;
                ins  = *(void**)((u8*)ins + 0x0C);
            }

            if (prev == NULL) {
                // Insert at head
                *(void**)((u8*)hdr + 0x0C) = h->free;
                h->free = hdr;
                *(void**)((u8*)hdr + 0x08) = NULL;
                if (*(void**)((u8*)hdr + 0x0C)) {
                    *(void**)((u8*)*(void**)((u8*)hdr + 0x0C) + 0x08) = hdr;
                }
            } else {
                *(void**)((u8*)hdr + 0x08) = prev;
                *(void**)((u8*)hdr + 0x0C) = *(void**)((u8*)prev + 0x0C);
                *(void**)((u8*)prev + 0x0C) = hdr;
                nxt = *(void**)((u8*)hdr + 0x0C);
                if (nxt) {
                    *(void**)((u8*)nxt + 0x08) = hdr;
                }
            }

            // Merge hdr with the block immediately after it, if adjacent
            nxt = *(void**)((u8*)hdr + 0x0C);
            if (nxt != NULL) {
                u32 hdrEnd = (u32)hdr + *(s32*)((u8*)hdr + 0x04) + IOS_HEAP_HDR_SIZE;
                if (hdrEnd == (u32)nxt) {
                    void* nxtNext = *(void**)((u8*)nxt + 0x0C);
                    *(void**)((u8*)hdr + 0x0C) = nxtNext;
                    if (nxtNext) *(void**)((u8*)nxtNext + 0x08) = hdr;
                    *(s32*)((u8*)hdr + 0x04) += *(s32*)((u8*)nxt + 0x04) + IOS_HEAP_HDR_SIZE;
                }
            }

            // Merge the block before hdr with hdr, if adjacent
            prev = *(void**)((u8*)hdr + 0x08);
            if (prev != NULL) {
                u32 prevEnd = (u32)prev + *(s32*)((u8*)prev + 0x04) + IOS_HEAP_HDR_SIZE;
                if (prevEnd == (u32)hdr) {
                    nxt = *(void**)((u8*)hdr + 0x0C);
                    *(void**)((u8*)prev + 0x0C) = nxt;
                    if (nxt) *(void**)((u8*)nxt + 0x08) = prev;
                    *(s32*)((u8*)prev + 0x04) += *(s32*)((u8*)hdr + 0x04) + IOS_HEAP_HDR_SIZE;
                }
            }

            result = IOS_OK;
        }
    }

done:
    OSRestoreInterrupts();
    return result;
}
