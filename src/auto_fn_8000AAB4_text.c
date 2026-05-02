#include <stdint.h>

/* Declare external global variables referenced in the assembly */
extern uint32_t lbl_8074E3A8;
extern uint32_t lbl_80563100;
extern uint32_t lbl_8074E3B0;
extern uint32_t lbl_8074E3B8;

/* Declare external functions referenced in the assembly */
extern void fn_80008C74(uint32_t* arg);
extern void __register_global_object(uint32_t* arg1, uint32_t* arg2);
extern void fn_80008CB0();

void fn_8000AAB4() {
    /* Save stack pointer and link register (simulated via stack adjustment) */
    /* Adjust stack by 0x10 bytes */
    /* Note: Actual stack adjustment would typically be handled by the compiler,
       but this is a simplified representation. */

    /* Load address of lbl_8074E3A8 into r3 */
    uint32_t* arg1 = &lbl_8074E3A8;

    /* Call fn_80008C74 with arg1 as argument */
    fn_80008C74(arg1);

    /* Load address of fn_80008CB0 into r4 */
    uint32_t* arg2 = (uint32_t*)0x80008CB0;

    /* Load address of lbl_80563100 into r5 */
    uint32_t* arg3 = &lbl_80563100;

    /* Call __register_global_object with arg1 and arg2 as arguments */
    __register_global_object(arg1, arg2);

    /* Zero out memory at lbl_8074E3B0 and lbl_8074E3B8 */
    *(volatile uint32_t*)(&lbl_8074E3B0) = 0;
    *(volatile uint32_t*)(&lbl_8074E3B8) = 0;
}
