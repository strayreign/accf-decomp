#include <stdio.h>

/*  Define the struct based on the assembly offsets */
typedef struct {
    float field1;
    float field2;
    float field3;
    float field4;
} MyStruct;

void fn_800631F8() {
    /*  Declare a variable of the struct type */
    MyStruct myStruct;

    /*  Initialize the fields using the given assembly offsets */
    __asm__ volatile (
        "lfs %0, 0(%1)\n"
        "stfs %2, 0(%3)\n"
        "lfs %4, 4(%1)\n"
        "stfs %5, 4(%6)\n"
        "lfs %7, 8(%1)\n"
        "stfs %8, 8(%9)\n"
        "lfs %10, 12(%1)\n"
        "stfs %11, 12(%13)\n"
        : "=f" (myStruct.field1), "=r" (myStruct),
          "=f" (myStruct.field2), "=r" (myStruct),
          "=f" (myStruct.field3), "=r" (myStruct),
          "=f" (myStruct.field4), "=r" (myStruct)
        : "m" (myStruct.field1), "m" (myStruct),
          "m" (myStruct.field2), "m" (myStruct),
          "m" (myStruct.field3), "m" (myStruct),
          "m" (myStruct.field4)
    );

    /*  Call the function to register global object */
    __register_global_object(&myStruct);
}

int main() {
    fn_800631F8();
    return 0;
}
