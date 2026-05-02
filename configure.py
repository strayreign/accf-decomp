#!/usr/bin/env python3

###
# Generates build files for the project.
# This file also includes the project configuration,
# such as compiler flags and the object matching status.
#
# Usage:
#   python3 configure.py
#   ninja
#
# Append --help to see available options.
###

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Union
from tools.project import *

from tools.defines_common import (
    cflags_includes,
    DEFAULT_VERSION,
    VERSIONS,
)

parser = argparse.ArgumentParser()
parser.add_argument(
    "mode",
    choices=["configure", "progress"],
    default="configure",
    help="script mode (default: configure)",
    nargs="?",
)
parser.add_argument(
    "-v",
    "--version",
    choices=VERSIONS,
    type=str.upper,
    default=VERSIONS[DEFAULT_VERSION],
    help="version to build",
)
parser.add_argument(
    "--build-dir",
    metavar="DIR",
    type=Path,
    default=Path("build"),
    help="base build directory (default: build)",
)
parser.add_argument(
    "--binutils",
    metavar="BINARY",
    type=Path,
    help="path to binutils (optional)",
)
parser.add_argument(
    "--compilers",
    metavar="DIR",
    type=Path,
    help="path to compilers (optional)",
)
parser.add_argument(
    "--map",
    action="store_true",
    help="generate map file(s)",
)
parser.add_argument(
    "--debug",
    action="store_true",
    help="build with debug info (non-matching)",
)
if not is_windows():
    parser.add_argument(
        "--wrapper",
        metavar="BINARY",
        type=Path,
        help="path to wibo or wine (optional)",
    )
parser.add_argument(
    "--dtk",
    metavar="BINARY | DIR",
    type=Path,
    help="path to decomp-toolkit binary or source (optional)",
)
parser.add_argument(
    "--objdiff",
    metavar="BINARY | DIR",
    type=Path,
    help="path to objdiff-cli binary or source (optional)",
)
parser.add_argument(
    "--sjiswrap",
    metavar="EXE",
    type=Path,
    help="path to sjiswrap.exe (optional)",
)
parser.add_argument(
    "--verbose",
    action="store_true",
    help="print verbose output",
)
parser.add_argument(
    "--non-matching",
    dest="non_matching",
    action="store_true",
    help="builds equivalent (but non-matching) or modded objects",
)
parser.add_argument(
    "--no-progress",
    dest="progress",
    action="store_false",
    help="disable progress calculation",
)
args = parser.parse_args()

config = ProjectConfig()
config.version = str(args.version)
version_num = VERSIONS.index(config.version)

# Apply arguments
config.build_dir = args.build_dir
config.dtk_path = args.dtk
config.objdiff_path = args.objdiff
config.binutils_path = args.binutils
config.compilers_path = args.compilers
config.generate_map = args.map
config.non_matching = args.non_matching
config.sjiswrap_path = args.sjiswrap
config.progress = args.progress
if not is_windows():
    config.wrapper = args.wrapper
# Don't build asm unless we're --non-matching
if not config.non_matching:
    config.asm_dir = None

# ── Local CodeWarrior for Wii v1.7 auto-setup ────────────────────────────────
# If CW for Wii v1.7 is installed, populate build/compilers/GC/1.3.2/ from it
# so the build system never has to download the compiler from decomp.dev.
# This runs once on first configure; subsequent runs skip the copy if already done.
def _setup_cw_local(build_dir: Path) -> Optional[Path]:
    """
    Copy mwcceppc.exe, mwldeppc.exe, and all support DLLs from the local
    CodeWarrior for Wii v1.7 installation into build/compilers/GC/1.3.2/.
    Returns the compilers root Path on success, None if CW is not found.
    Runs once; subsequent calls are a no-op if the destination already exists.
    """
    import shutil as _sh

    _CW_ROOTS = [
        Path("C:/Program Files/Freescale/CW for Wii v1.7"),
        Path("C:/Program Files (x86)/Freescale/CW for Wii v1.7"),
    ]
    # Known sub-paths where the PowerPC command-line tools live in CW for Wii v1.7
    _CW_SUBPATHS = [
        Path("PowerPC_EABI_Tools/Command_Line_Tools"),
        Path("PowerPC_EABI_Tools/Bin"),
        Path("Bin"),
        Path("PowerPC_EABI_Tools"),
    ]

    # Fast path: check known locations first
    cw_tools: Optional[Path] = None
    for root in _CW_ROOTS:
        for sub in _CW_SUBPATHS:
            candidate = root / sub
            if (candidate / "mwcceppc.exe").exists():
                cw_tools = candidate
                break
        if cw_tools:
            break

    # Fallback: recursive search under each CW root (handles unexpected structures)
    if cw_tools is None:
        for root in _CW_ROOTS:
            if not root.exists():
                continue
            for exe in root.rglob("mwcceppc.exe"):
                cw_tools = exe.parent
                break
            if cw_tools:
                break

    if cw_tools is None:
        return None  # CW not installed; fall through to decomp.dev download

    dst = build_dir / "compilers" / "GC" / "1.3.2"
    if (dst / "mwcceppc.exe").exists():
        return build_dir / "compilers"  # already set up from a previous configure run

    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    # Copy the tools directory itself
    for f in cw_tools.iterdir():
        if f.is_file():
            try:
                _sh.copy2(f, dst / f.name)
                copied.append(f.name)
            except Exception:
                pass

    # Also sweep sibling/parent directories for support DLLs that mwcc needs at runtime
    for rel in (".", "..", "../Bin", "../Lib", "Bin", "Lib"):
        sib = (cw_tools / rel).resolve()
        if sib == cw_tools or not sib.is_dir():
            continue
        for f in sib.iterdir():
            if f.is_file() and f.suffix.lower() == ".dll" and not (dst / f.name).exists():
                try:
                    _sh.copy2(f, dst / f.name)
                    copied.append(f.name)
                except Exception:
                    pass

    if not (dst / "mwcceppc.exe").exists():
        print(f"[configure] WARNING: copy failed — mwcceppc.exe not in {dst}")
        return None

    print(f"[configure] CodeWarrior for Wii v1.7 → {dst}  ({len(copied)} files copied)")
    print(f"[configure]   source: {cw_tools}")
    return build_dir / "compilers"

# Only auto-detect when --compilers was not explicitly passed on the command line
if config.compilers_path is None:
    _local_cw = _setup_cw_local(args.build_dir)
    if _local_cw is not None:
        config.compilers_path = _local_cw
        config.compilers_tag = None  # prevent decomp.dev download
        print(f"[configure] Using local CodeWarrior for Wii v1.7 ({config.compilers_path})")

# Tool versions
config.binutils_tag = "2.42-2"
# compilers_tag drives the decomp.dev download; clear it when using local CW
config.compilers_tag = None if config.compilers_path else "20251118"
config.dtk_tag = "v1.8.3"
config.objdiff_tag = "v3.6.1"
config.sjiswrap_tag = "v1.2.2"
config.wibo_tag = "1.0.3"

# Project
config_dir = Path("config") / config.version
config_json_path = config_dir / "config.json"
objects_path = config_dir / "objects.json"
config.config_path = config_dir / "config.yml"
config.check_sha_path = config_dir / "build.sha1"
config.reconfig_deps = [
    config_json_path,
    objects_path,
]

# Optional numeric ID for decomp.me preset
config.scratch_preset_id = None

# Build flags
flags = json.load(open(config_json_path, "r", encoding="utf-8"))
progress_categories: dict[str, str] = flags["progress_categories"]
asflags: list[str] = flags["asflags"]
ldflags: list[str] = flags["ldflags"]
cflags: dict[str, dict] = flags["cflags"]


def get_cflags(name: str) -> list[str]:
    return cflags[name]["flags"]


def add_cflags(name: str, extra: list[str]):
    cflags[name]["flags"] = [*extra, *cflags[name]["flags"]]


def get_cflags_base(name: str) -> str:
    return cflags[name].get("base", None)


def are_cflags_inherited(name: str) -> bool:
    return "inherited" in cflags[name]


def set_cflags_inherited(name: str):
    cflags[name]["inherited"] = True


def apply_base_cflags(key: str):
    if are_cflags_inherited(key):
        return

    base = get_cflags_base(key)
    if base is None:
        add_cflags(key, cflags_includes)
    else:
        apply_base_cflags(base)
        add_cflags(key, get_cflags(base))

    set_cflags_inherited(key)


# Set up base flags
base_cflags = get_cflags("base")
base_cflags.append(f"-i build/{config.version}/include")
base_cflags.append(f"-DBUILD_VERSION={version_num}")
base_cflags.append(f"-DVERSION_{config.version}")

# Set conditionally-added flags
if args.debug:
    base_cflags.extend(["-sym on", "-DDEBUG=1"])
else:
    base_cflags.append("-DNDEBUG=1")

# ldflags
if args.debug:
    ldflags.append("-g")
if config.generate_map:
    ldflags.append("-mapunused")

# Apply cflag inheritance
for key in cflags.keys():
    apply_base_cflags(key)

config.asflags = [
    *asflags,
    "-I include",
    f"-I build/{config.version}/include",
    f"--defsym BUILD_VERSION={version_num}",
    f"--defsym VERSION_{config.version}",
]
config.ldflags = ldflags

config.linker_version = "GC/1.3.2"

config.warn_missing_config = True
config.warn_missing_source = False

# Object files
Matching = True
Equivalent = config.non_matching
NonMatching = False


def get_object_completed(status: str) -> bool:
    if status == "MISSING":
        return NonMatching
    elif status == "Matching":
        return Matching
    elif status == "NonMatching":
        return NonMatching
    elif status == "Equivalent":
        return Equivalent
    elif status == "LinkIssues":
        return NonMatching

    assert False, f"Invalid object status {status}"


libs: list[dict] = []
objects: dict[str, dict] = json.load(open(objects_path, "r", encoding="utf-8"))
for lib, lib_config in objects.items():
    config_cflags: list[str] = lib_config.pop("cflags")
    lib_cflags = get_cflags(config_cflags) if isinstance(config_cflags, str) else config_cflags

    lib_objects: list[Object] = []
    config_objects: dict[str, Union[str, dict[str, Union[str, Any]]]] = lib_config.pop("objects")
    if len(config_objects) < 1:
        continue

    for path, obj_config in config_objects.items():
        if isinstance(obj_config, str):
            completed = get_object_completed(obj_config)
            lib_objects.append(Object(completed, path))
        else:
            completed = get_object_completed(obj_config["status"])

            if "cflags" in obj_config:
                object_cflags = obj_config["cflags"]
                if isinstance(object_cflags, str):
                    obj_config["cflags"] = get_cflags(object_cflags)

            lib_objects.append(Object(completed, path, **obj_config))

    libs.append(
        {
            "lib": lib,
            "cflags": lib_cflags,
            "host": False,
            "objects": lib_objects,
            **lib_config,
        }
    )

config.libs = libs

# Progress tracking categories
config.progress_categories = [
    ProgressCategory(name, desc) for (name, desc) in progress_categories.items()
]
config.progress_each_module = args.verbose

if args.mode == "configure":
    # Write build.ninja and objdiff.json
    generate_build(config)
elif args.mode == "progress":
    # Print progress and write progress.json
    calculate_progress(config)
else:
    sys.exit("Unknown mode: " + args.mode)
