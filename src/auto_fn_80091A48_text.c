#include <stdint.h>

typedef struct {
    uint32_t field_0x0;
} ColorFader;

void fn_80091A48(void) {
    uint32_t local_0x14 = 0;
    ColorFader *local_0x10 = (ColorFader *)fn_80085E8C(&local_0x14);
    __register_global_object(fn_80091A9C, &local_0x10->field_0x0);
}
