#!/usr/bin/env python3
# autopilot.py -- compatibility shim; the real script is tools/tom_hook.py
import runpy, sys
from pathlib import Path
sys.argv[0] = str(Path(__file__).parent / "tom_hook.py")
runpy.run_path(str(Path(__file__).parent / "tom_hook.py"), run_name="__main__")
