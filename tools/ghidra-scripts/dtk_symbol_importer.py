# Import labels from a dtk symbols.txt
#@author Dark
#@category Import
#@menupath Tools.Import From dtk Symbols File

import subprocess

from ghidra.program.model.symbol import *
from ghidra.program.model.symbol import SourceType, SymbolType, SymbolUtilities

# Read/demangle symbols file
f = askFile("Symbols File", "OK")
demangler_output = subprocess.check_output(['powerpc-eabi-c++filt', '--format=gnu-v3'], input=open(f.absolutePath, 'rb').read())

# Delete non-primary/user existing symbols, as we'll be replacing them
symbolTable = currentProgram.getSymbolTable()
for sym in symbolTable.getSymbolIterator():
    if sym.getSource() == SourceType.USER_DEFINED:
        continue
    if sym.isPrimary():
        continue
    sym.delete()

# Apply symbols to the program
for line in demangler_output.splitlines():
    line = line.decode("utf-8").strip()
    if not line or line.startswith("//"):
        continue

    tokens = line.split()
    if len(tokens) < 2:
        continue

    addr_str = tokens[0]
    name = tokens[1]

    try:
        address = toAddr(int(addr_str, 16))
        symbolTable.createLabel(address, name, SourceType.USER_DEFINED)
        print(f"Created label {name} at {addr_str}")
    except Exception as e:
        print(f"Failed to create label {name} at {addr_str}: {e}")
