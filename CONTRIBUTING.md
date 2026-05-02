# Contributing

## Learning

Decompilation is a fairly advanced skill and can potentially take a long time to
learn. Having prior programming experience (especially with C++) is highly recommended.

If you want an overview of how the process works, take a peek at [MattKC's
video](https://youtu.be/MToTEqoVv3I?feature=shared) for the Lego Island decompilation
project. While not everything covered applies directly to this project, it provides a
good summary of how decompilation projects work.

## Ghidra

We use [Ghidra](https://ghidra-sre.org/) with the
[GameCube loader extension](https://github.com/Cuyler36/Ghidra-GameCube-Loader) for
reverse engineering work. A script for reimporting symbols from `symbols.txt` into
your local Ghidra project is available in `tools/ghidra-scripts/`.

## Text Editors

For newcomers, we recommend [VSCode](https://code.visualstudio.com/) with Microsoft's
C++ plugin. You may use any text editor you prefer as long as you have a good way of
running `clang-format`.

## Guidelines

### Code formatting

Any code that is not from a third-party library should be formatted with `clang-format`
before being submitted. Run `python3 reformat.py` to format all source files at once.
This avoids style debates and keeps the focus on decompilation accuracy.

### Continuous Integration

We use GitHub Actions to verify all merged code produces a matching binary. Fix any
CI failures before requesting a merge.

### Naming conventions

Follow what surrounding code does. When in doubt, check the symbols list or ask in
the relevant decompilation Discord.

## Further questions

If you have further questions, feel free to join the
[GC/Wii Decompilation Discord](https://discord.gg/hKx3FJJgrV).
