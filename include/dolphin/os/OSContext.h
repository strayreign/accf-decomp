#ifndef DOLPHIN_OSCONTEXT_H
#define DOLPHIN_OSCONTEXT_H

#ifdef __cplusplus
extern "C" {
#endif

// Placeholder for OSContext structure
// Full definition will be added as needed

typedef struct OSContext {
    /* Context register fields - size depends on architecture */
    /* Placeholder for now */
    char _padding[0x2B0]; // Adjust size as needed
} OSContext;

#ifdef __cplusplus
}
#endif

#endif // DOLPHIN_OSCONTEXT_H
