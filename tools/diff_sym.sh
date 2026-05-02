#!/bin/sh
git --no-pager diff --no-index \
	<(build/tools/dtk elf disasm --symbol=$1 build/RUUE01/main.elf) \
	<(build/tools/dtk elf disasm --symbol=$1 orig/RUUE01/sys/main.dol)
