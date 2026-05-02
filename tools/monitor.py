#!/usr/bin/env python3
"""
monitor.py — Post-push CI and decomp.dev watcher.

Automatically called by decomp_loop.py after every git push.
Also useful standalone:

  python3 tools/monitor.py            # watch latest run until complete
  python3 tools/monitor.py --once     # print current status and exit
  python3 tools/monitor.py --loop     # keep watching indefinitely (run after each push)

Requirements:
  pip install httpx
  export GITHUB_TOKEN=ghp_...    (needs repo:read + actions:read scope)
"""

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GITHUB_REPO   = "strayreign/accf-decomp"
DECOMP_DEV_URL = "https://decomp.dev/strayreign/accf-decomp"

# How often to poll GitHub Actions (seconds)
POLL_INTERVAL = 10
# Give up waiting after this many seconds
POLL_TIMEOUT  = 600  # 10 minutes


# ─── GitHub Actions ───────────────────────────────────────────────────────────

def get_github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"Accept": "application/vnd.github+json"}
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }


def fetch_latest_run(client) -> dict | None:
    """Return the most recent workflow run object, or None."""
    resp = client.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
        params={"per_page": 1},
        headers=get_github_headers(),
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  ⚠  GitHub API returned {resp.status_code}: {resp.text[:200]}")
        return None
    runs = resp.json().get("workflow_runs", [])
    return runs[0] if runs else None


def format_run(run: dict) -> str:
    status     = run.get("status", "?")
    conclusion = run.get("conclusion") or "—"
    name       = run.get("name", "?")
    branch     = run.get("head_branch", "?")
    sha        = run.get("head_sha", "")[:7]
    url        = run.get("html_url", "")
    created    = run.get("created_at", "?")[:19].replace("T", " ")

    icon = {
        ("completed", "success"):   "✅",
        ("completed", "failure"):   "❌",
        ("completed", "cancelled"): "⊘",
        ("in_progress", None):      "🔄",
        ("queued", None):           "⏳",
    }.get((status, run.get("conclusion")), "❓")

    return (
        f"  {icon}  {name} [{branch}@{sha}]  "
        f"status={status}/{conclusion}  "
        f"created={created}\n"
        f"     {url}"
    )


def watch_actions(once: bool = False, pushed_at: float | None = None) -> bool:
    """
    Poll GitHub Actions until a run triggered AFTER pushed_at finishes.
    If pushed_at is None, uses current time so we never pick up stale runs.
    Returns True if the run succeeded.
    """
    try:
        import httpx
    except ImportError:
        print("  ⚠  httpx not installed — pip install httpx")
        return False

    # Only consider runs created after this UTC timestamp
    if pushed_at is None:
        pushed_at = time.time()

    import datetime
    min_dt = datetime.datetime.fromtimestamp(pushed_at, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("  ⚠  GITHUB_TOKEN not set — cannot check CI")
        return False
    print(f"  🔑  GITHUB_TOKEN: {token[:8]}…")

    print(f"\n  🔍  GitHub Actions — {GITHUB_REPO}")
    print(f"  ⏳  Waiting for run triggered after {min_dt} …")

    with httpx.Client() as client:
        start = time.time()

        while True:
            run = fetch_latest_run(client)

            # Skip runs that predate the push
            if run:
                run_created = run.get("created_at", "")
                if run_created <= min_dt:
                    # New run hasn't appeared yet
                    if time.time() - start > POLL_TIMEOUT:
                        print(f"  ⏰  Timed out waiting for new run after {POLL_TIMEOUT}s")
                        return False
                    print(f"  ⏳  No new run yet (latest: {run_created}) — waiting …")
                    time.sleep(POLL_INTERVAL)
                    continue

            if not run:
                print("  ⚠  Could not fetch runs (check GITHUB_TOKEN)")
                return False

            print(format_run(run))

            if once:
                return run.get("conclusion") == "success"

            status = run.get("status", "")
            if status == "completed":
                ok = run.get("conclusion") == "success"
                if ok:
                    print("  ✅  Build passed!")
                    delete_old_runs(client, keep_run_id=run["id"])
                else:
                    print(f"  ❌  Build {run.get('conclusion')} — check logs above")
                return ok

            if time.time() - start > POLL_TIMEOUT:
                print(f"  ⏰  Timed out after {POLL_TIMEOUT}s")
                return False

            time.sleep(POLL_INTERVAL)


# ─── decomp.dev scraper ───────────────────────────────────────────────────────

def fetch_decomp_dev(client) -> dict:
    """Scrape overall progress stats from decomp.dev."""
    import re
    resp = client.get(DECOMP_DEV_URL, follow_redirects=True, timeout=15)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}

    text = resp.text

    # Overall percentage
    pct_m = re.search(r"([\d.]+)\s*%\s*(?:decompiled|matched)", text, re.I)
    pct   = float(pct_m.group(1)) if pct_m else None

    # Code / Data sizes
    code_m = re.search(r"Code[^(]*\(([\d.]+)\s*([KMG]?B)\)", text)
    data_m = re.search(r"Data[^(]*\(([\d.]+)\s*([KMG]?B)\)", text)
    code   = f"{code_m.group(1)} {code_m.group(2)}" if code_m else None
    data   = f"{data_m.group(1)} {data_m.group(2)}" if data_m else None

    return {"pct": pct, "code": code, "data": data}


def fetch_decomp_dev_treemap(client) -> list[dict]:
    """
    Scrape the treemap canvas script to extract per-unit progress URLs.
    decomp.dev renders a <canvas id="treemap"> whose data is driven by
    inline JSON/JS. We extract unit names and their match percentages.
    Returns list of {"unit": str, "pct": float, "url": str}.
    """
    import re
    resp = client.get(DECOMP_DEV_URL, follow_redirects=True, timeout=15)
    if resp.status_code != 200:
        return []

    text = resp.text

    # Look for the JS array that backs the treemap — format varies but
    # typically looks like: {"name":"main/auto_03_...","value":X.XX, ...}
    units = []

    # Pattern 1: JSON-ish object with name + value pairs
    pattern = re.compile(
        r'"name"\s*:\s*"(main/[^"]+)"[^}]*?"(?:value|percent|match)"\s*:\s*([\d.]+)',
        re.DOTALL
    )
    for m in pattern.finditer(text):
        unit_full = m.group(1)
        pct       = float(m.group(2))
        unit_enc  = unit_full.replace("/", "%2F")
        units.append({
            "unit": unit_full,
            "pct":  pct,
            "url":  f"{DECOMP_DEV_URL}?unit={unit_enc}",
        })

    # Pattern 2: data-unit attributes or query string fragments
    if not units:
        frag_pat = re.compile(r'unit=(main[^"&\s]+)')
        for m in frag_pat.finditer(text):
            raw = m.group(1).replace("%2F", "/")
            units.append({
                "unit": raw,
                "pct":  None,
                "url":  f"{DECOMP_DEV_URL}?unit={m.group(1)}",
            })

    return units


def show_decomp_dev(prev_pct: float | None = None) -> float | None:
    """Print current decomp.dev stats. Returns current pct."""
    try:
        import httpx
    except ImportError:
        print("  ⚠  httpx not installed")
        return None

    print(f"\n  🌐  decomp.dev — {DECOMP_DEV_URL}")

    with httpx.Client() as client:
        stats = fetch_decomp_dev(client)

    if "error" in stats:
        print(f"  ⚠  {stats['error']}")
        return None

    pct  = stats["pct"]
    code = stats.get("code", "?")
    data = stats.get("data", "?")

    if pct is not None:
        diff_str = ""
        if prev_pct is not None and pct != prev_pct:
            delta = pct - prev_pct
            diff_str = f"  (Δ {delta:+.3f}%)"
        print(f"  📈  {pct:.4f}% decompiled{diff_str}")
        print(f"  💾  Code: {code}   Data: {data}")
    else:
        print("  ⚠  Could not parse percentage")

    return pct


def read_baseline_pct() -> float | None:
    """Read the last-recorded decomp.dev % from a local cache file."""
    cache = PROJECT_ROOT / "tools" / ".decomp_dev_pct"
    if cache.exists():
        try:
            return float(cache.read_text().strip())
        except ValueError:
            pass
    return None


def write_baseline_pct(pct: float):
    cache = PROJECT_ROOT / "tools" / ".decomp_dev_pct"
    cache.write_text(str(pct))


# ─── Actions run cleanup ──────────────────────────────────────────────────────

def delete_old_runs(client, keep_run_id: int) -> None:
    """
    Delete all workflow runs except keep_run_id.
    Requires GITHUB_TOKEN with Actions: write scope.
    """
    headers = get_github_headers()
    page    = 1
    deleted = 0
    errors  = 0

    print(f"\n  🗑   Deleting old Actions runs (keeping #{keep_run_id}) …")

    while True:
        resp = client.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
            params={"per_page": 100, "page": page},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  ⚠  Could not list runs: HTTP {resp.status_code}")
            return

        runs = resp.json().get("workflow_runs", [])
        if not runs:
            break

        for run in runs:
            rid = run["id"]
            if rid == keep_run_id:
                continue
            # Only delete completed runs (can't delete in-progress)
            if run.get("status") != "completed":
                continue
            dr = client.delete(
                f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{rid}",
                headers=headers,
                timeout=15,
            )
            if dr.status_code in (204, 404):
                deleted += 1
            else:
                errors += 1
                print(f"  ⚠  Could not delete run #{rid}: HTTP {dr.status_code} — {dr.text[:120]}")

        if len(runs) < 100:
            break
        page += 1

    print(f"  ✅  Deleted {deleted} run(s){f', {errors} error(s)' if errors else ''}.")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Watch GitHub Actions CI and decomp.dev after a git push."
    )
    parser.add_argument("--once",  action="store_true",
                        help="Print current status and exit immediately")
    parser.add_argument("--loop",  action="store_true",
                        help="Poll continuously until manually stopped")
    parser.add_argument("--no-actions", action="store_true",
                        help="Skip GitHub Actions, only show decomp.dev")
    parser.add_argument("--no-decomp-dev", action="store_true",
                        help="Skip decomp.dev, only show Actions")
    parser.add_argument("--pushed-at", type=float, default=None,
                        help="Unix timestamp of push; ignore runs created before this")
    args = parser.parse_args()

    prev_pct = read_baseline_pct()

    if not args.no_actions:
        ok = watch_actions(once=args.once, pushed_at=args.pushed_at)
        if not ok and not args.once:
            print("  Build did not succeed — skipping decomp.dev update")
            sys.exit(1)

    if not args.no_decomp_dev:
        pct = show_decomp_dev(prev_pct)
        if pct is not None:
            write_baseline_pct(pct)

    if args.loop:
        print("\n  Waiting 60s before next check …")
        time.sleep(60)
        main()


if __name__ == "__main__":
    main()
