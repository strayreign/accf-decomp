# cleanup_win.ps1 — Remove macOS artifacts, Python bytecache, and stale locks
# Run from the project root:
#   .\tools\cleanup_win.ps1

Set-Location (Split-Path $PSScriptRoot -Parent)
$root = Get-Location

Write-Host "`n=== ACCF decomp cleanup ===" -ForegroundColor Cyan

# ── macOS .DS_Store files ─────────────────────────────────────────────────────
$ds = Get-ChildItem -Recurse -Force -Filter ".DS_Store" | Where-Object { $_.FullName -notmatch "\\.git\\" }
if ($ds) {
    $ds | Remove-Item -Force
    Write-Host "  Removed $($ds.Count) .DS_Store file(s)" -ForegroundColor Green
} else {
    Write-Host "  No .DS_Store files found" -ForegroundColor DarkGray
}

# ── Python __pycache__ directories ───────────────────────────────────────────
$pc = Get-ChildItem -Recurse -Force -Filter "__pycache__" -Directory | Where-Object { $_.FullName -notmatch "\\.git\\" }
if ($pc) {
    $pc | Remove-Item -Recurse -Force
    Write-Host "  Removed $($pc.Count) __pycache__ dir(s)" -ForegroundColor Green
} else {
    Write-Host "  No __pycache__ dirs found" -ForegroundColor DarkGray
}

# ── Ghidra stale lock files ───────────────────────────────────────────────────
$locks = Get-ChildItem -Path "ghidra_projects" -Recurse -Force -Include "*.lock","*.lock~" -ErrorAction SilentlyContinue
if ($locks) {
    $locks | Remove-Item -Force
    Write-Host "  Removed $($locks.Count) Ghidra lock file(s)" -ForegroundColor Green
} else {
    Write-Host "  No Ghidra lock files found" -ForegroundColor DarkGray
}

# ── Duplicate empty Ghidra project (ghidra_projects\accf\) ───────────────────
# The real project is ghidra_projects\accf.gpr + accf.rep\ at the root.
# The nested accf\ folder is an empty duplicate with no program data.
$dupProj = "ghidra_projects\accf"
if (Test-Path $dupProj) {
    Remove-Item -Recurse -Force $dupProj
    Write-Host "  Removed empty duplicate Ghidra project: $dupProj" -ForegroundColor Green
}

# ── Stale git lock files ──────────────────────────────────────────────────────
$gitLocks = @(
    ".git\refs\tmp_rewrite.lock",
    ".git\objects\maintenance.lock"
) | Where-Object { Test-Path $_ }
if ($gitLocks) {
    $gitLocks | ForEach-Object { Remove-Item -Force $_; Write-Host "  Removed git lock: $_" -ForegroundColor Green }
} else {
    Write-Host "  No stale git locks found" -ForegroundColor DarkGray
}

Write-Host "`nDone." -ForegroundColor Cyan
