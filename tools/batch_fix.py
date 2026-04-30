#!/usr/bin/env python3
"""
batch_fix.py — Run decomp_loop.py for every unmatched text unit.

Usage:
  python3 tools/batch_fix.py                  # all incomplete text units
  python3 tools/batch_fix.py --min 50         # only units >= 50% (almost done)
  python3 tools/batch_fix.py --max-attempts 6 # more attempts per unit
  python3 tools/batch_fix.py --dry-run        # show what would run, don't do it
  python3 tools/batch_fix.py --no-commit      # don't git commit on matches

Recommended overnight run:
  nohup python3 tools/batch_fix.py --max-attempts 4 > logs/batch.log 2>&1 &
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_JSON  = PROJECT_ROOT / "build" / "RUUE01" / "report.json"
OBJDIFF_CLI  = PROJECT_ROOT / "build" / "tools" / "objdiff-cli"
LOOP_SCRIPT  = PROJECT_ROOT / "tools" / "decomp_loop.py"
HISTORY_FILE = PROJECT_ROOT / "tools" / "model_history.json"
LOGS_DIR     = PROJECT_ROOT / "logs"

GITHUB_REPO  = "strayreign/accf-decomp"


def regenerate_report():
    """Rebuild report.json from current object files so the unit list is accurate."""
    if not OBJDIFF_CLI.exists():
        return
    subprocess.run(
        [str(OBJDIFF_CLI), "report", "generate", "-o", str(REPORT_JSON)],
        cwd=PROJECT_ROOT, capture_output=True,
    )


def delete_all_actions_except_latest():
    """Delete all GitHub Actions runs except the most recent one."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    try:
        import httpx
    except ImportError:
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    with httpx.Client() as client:
        resp = client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
            params={"per_page": 100}, headers=headers, timeout=15,
        )
        if resp.status_code != 200:
            return
        runs = resp.json().get("workflow_runs", [])
        if len(runs) <= 1:
            return
        keep_id = runs[0]["id"]
        deleted = 0
        for run in runs[1:]:
            if run.get("status") != "completed":
                continue
            r = client.delete(
                f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run['id']}",
                headers=headers, timeout=15,
            )
            if r.status_code in (204, 404):
                deleted += 1
        if deleted:
            print(f"  🗑   Deleted {deleted} old Actions run(s) (keeping #{keep_id})\n")


def next_model_for(unit_name: str, all_model_ids: list[str]) -> str | None:
    """
    If this unit was previously attempted and didn't match, return the model id
    one level above the last one tried. Returns None if no history or already
    at the top of the ladder.
    """
    if not HISTORY_FILE.exists():
        return None
    with open(HISTORY_FILE) as f:
        history = json.load(f)
    entry = history.get("units", {}).get(unit_name)
    if not entry or entry.get("matched"):
        return None
    last_level = entry.get("model_level", -1)
    next_level = last_level + 1
    if next_level >= len(all_model_ids):
        return None  # already tried everything
    return all_model_ids[next_level]


def load_incomplete_units(min_pct: float = 0.0, max_pct: float = 99.9) -> list[dict]:
    """Return list of {name, pct} for text units below 100%."""
    with open(REPORT_JSON) as f:
        report = json.load(f)

    results = []
    for unit in report.get("units", []):
        name = unit.get("name", "")
        # Only text sections; skip data/bss/etc
        if "_text" not in name:
            continue
        m = unit.get("measures", {})
        pct = m.get("fuzzy_match_percent", 100.0)
        if min_pct <= pct < 100.0 and pct <= max_pct:
            results.append({
                "name": name,
                "short": name.replace("main/", ""),
                "pct": pct,
                "total_code": m.get("total_code", "?"),
                "total_functions": m.get("total_functions", "?"),
            })

    # Work through easiest (highest %) first — quickest wins
    results.sort(key=lambda x: -x["pct"])
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Batch-run decomp_loop.py for all unmatched ACCF text units."
    )
    parser.add_argument(
        "--min", type=float, default=0.0, metavar="PCT",
        help="Skip units below this match %% (e.g. --min 50 for nearly-done units)",
    )
    parser.add_argument(
        "--max", type=float, default=99.9, metavar="PCT",
        help="Skip units above this match %% (default 99.9 = everything unmatched)",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=4,
        help="Max LLM attempts per unit (passed through to decomp_loop.py)",
    )
    parser.add_argument(
        "--sleep", type=float, default=2.0,
        help="Seconds to sleep between units (lets GPU breathe, default 2)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List units that would be processed, then exit",
    )
    parser.add_argument(
        "--no-commit", action="store_true",
        help="Don't git commit/push even on 100%% matches",
    )
    args = parser.parse_args()

    delete_all_actions_except_latest()
    regenerate_report()
    units = load_incomplete_units(min_pct=args.min, max_pct=args.max)

    # Read the model ladder from decomp_loop so we know the IDs
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("decomp_loop", LOOP_SCRIPT)
    _mod  = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    ALL_MODEL_IDS = [m["id"] for m in _mod.MODELS]

    if not units:
        print("✅ Nothing to do — all text units are at 100%!")
        return

    total_code = sum(
        int(u["total_code"]) for u in units if str(u["total_code"]).isdigit()
    )

    print(f"{'═'*62}")
    print(f"  Batch fix — {len(units)} unit(s) to process")
    print(f"  Total unmatched code: {total_code} bytes")
    print(f"{'═'*62}\n")

    for i, u in enumerate(units, 1):
        bar_pct  = int(u["pct"] / 5)
        bar      = "█" * bar_pct + "░" * (20 - bar_pct)
        print(f"  [{i:>3}/{len(units)}]  {u['short']:<40}  {u['pct']:5.1f}%  [{bar}]")

    if args.dry_run:
        print("\nDry run — exiting without calling decomp_loop.py")
        return

    # Re-run configure.py and patch build.ninja to use this Python interpreter.
    # After a squash-push all timestamps reset, so ninja would try to regenerate
    # build.ninja using the system /usr/bin/python3 (which lacks our packages).
    # We run it ourselves, patch the python= line, then touch so ninja is satisfied.
    import re as _re
    print("  🔧  Refreshing build.ninja …")
    cfg = subprocess.run(
        [sys.executable, "configure.py"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if cfg.returncode != 0:
        print(f"  ⚠  configure.py failed:\n{cfg.stderr[:300]}")
    ninja_file = PROJECT_ROOT / "build.ninja"
    if ninja_file.exists():
        text = ninja_file.read_text()
        patched = _re.sub(
            r'^python\s*=.*$', f'python = {sys.executable}',
            text, flags=_re.MULTILINE,
        )
        if patched != text:
            ninja_file.write_text(patched)
        ninja_file.touch()
        print("  ✅  build.ninja refreshed\n")

    # Set up log dir
    LOGS_DIR.mkdir(exist_ok=True)

    succeeded = []
    failed    = []

    for i, u in enumerate(units, 1):
        unit_name = u["short"]
        print(f"\n{'─'*62}")
        print(f"  [{i}/{len(units)}]  {unit_name}  (current: {u['pct']:.1f}%)")
        print(f"{'─'*62}")

        log_file = LOGS_DIR / f"{unit_name}.log"

        next_model = next_model_for(unit_name, ALL_MODEL_IDS)
        cmd = [
            sys.executable, "-u", str(LOOP_SCRIPT),
            unit_name,
            "--max-attempts", str(args.max_attempts),
        ]
        if next_model:
            print(f"  📚  History: resuming from {next_model} (skipping already-tried models)")
            cmd += ["--start-model", next_model]
        if args.no_commit:
            cmd.append("--no-commit")

        with open(log_file, "w") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,  # unbuffered
            )
            # Read one character at a time so dots/progress print instantly
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                sys.stdout.write(ch)
                sys.stdout.flush()
                log.write(ch)
            proc.wait()
            result = proc

        if result.returncode == 0:
            succeeded.append(unit_name)
            print(f"  ✅  {unit_name} — matched!")
        else:
            failed.append(unit_name)
            print(f"  ✗   {unit_name} — no match (log: {log_file.relative_to(PROJECT_ROOT)})")

        if i < len(units):
            time.sleep(args.sleep)

    # Summary
    print(f"\n{'═'*62}")
    print(f"  Batch complete")
    print(f"  ✅ Matched:    {len(succeeded)}")
    print(f"  ✗  Unmatched:  {len(failed)}")
    if succeeded:
        print(f"\n  Matched units:")
        for name in succeeded:
            print(f"    • {name}")
    if failed:
        print(f"\n  Still unmatched:")
        for name in failed:
            print(f"    • {name}")
    print(f"{'═'*62}")

    # Check if any failed units still have models left to try — if so, loop
    still_tryable = [
        u for u in failed
        if next_model_for(u, ALL_MODEL_IDS) is not None
    ]
    if still_tryable:
        print(f"\n  ♻️   {len(still_tryable)} unit(s) have higher models to try — restarting …\n")
        time.sleep(3)
        main()
    else:
        if failed:
            print("\n  🏁  All models exhausted on remaining units. Done.")
        else:
            print("\n  🎉  All units matched!")


if __name__ == "__main__":
    main()
