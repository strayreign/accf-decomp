#ifndef DOLPHIN_OSTHREAD_H
#define DOLPHIN_OSTHREAD_H

#include <dolphin/os/OSContext.h>

#ifdef __cplusplus
extern "C" {
#endif

struct OSThread;
struct OSMutex;

typedef struct OSThreadQueue {
    /* 0x00 */ struct OSThread* head;
    /* 0x04 */ struct OSThread* tail;
} OSThreadQueue;

typedef struct OSThreadLink {
    /* 0x00 */ struct OSThread* next;
    /* 0x04 */ struct OSThread* prev;
} OSThreadLink;

typedef struct OSMutexQueue {
    /* 0x00 */ struct OSMutex* head;
    /* 0x04 */ struct OSMutex* tail;
} OSMutexQueue;

typedef struct OSMutexLink {
    /* 0x00 */ struct OSMutex* next;
    /* 0x04 */ struct OSMutex* prev;
} OSMutexLink;

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSTHREAD_H
