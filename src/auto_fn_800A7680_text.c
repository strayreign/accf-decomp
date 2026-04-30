The key insight is that `r31` is being loaded TWICE (once implicitly in the first `lis/addi` pair, then again explicitly). The pattern shows:
1. First `lis r31, lbl_8058B97C@ha` + `addi r3, r31, lbl_8058B97C@l` loads the address into r3
2. Then `addi r31, r31, lbl_8058B97C@l` actually completes the load of r31
3. Then `addi r3, r31, 0x3c` uses r31

This is the standard two-instruction address-load pattern. The addressing must compute absolute addresses correctly using the high/low parts.
