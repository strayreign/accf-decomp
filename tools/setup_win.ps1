# setup_win.ps1 -- One-time setup for Windows
#   Hardware: Ryzen 7700X | RTX 5060 Ti 8GB GDDR7 (GPU 0) + GTX 1060 6GB (GPU 1) | 64GB DDR5
#
# Run from project root in an elevated PowerShell:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#   .\tools\setup_win.ps1

Write-Host "=== ACCF decomp Windows setup ===" -ForegroundColor Cyan

# ── Ollama ────────────────────────────────────────────────────────────────────
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Ollama..." -ForegroundColor Yellow
    winget install Ollama.Ollama --silent
    $env:PATH += ";$env:LOCALAPPDATA\Programs\Ollama"
}
Write-Host "Ollama: $(ollama --version 2>$null)" -ForegroundColor Green

# ── Pull models ───────────────────────────────────────────────────────────────
# Model ladder:
#   Tier 0  qwen2.5-coder:7b-instruct-q4_K_M   ~4.7GB  -> GTX 1060 6GB  (port 11435, fast pre-screener)
#   Tier 1  qwen2.5-coder:14b-instruct-q3_K_M  ~7.0GB  -> RTX 5060 Ti   (port 11434, fully in VRAM)
#   Tier 2  devstral-small-2:24b               ~15GB   -> both GPUs     (port 11434, dense heavy lifter)
#   Tier 3  qwen3:30b-a3b-q4_K_M               ~19GB   -> both GPUs     (port 11434, MoE alt; ~5GB CPU spill)
#   Tier 4  Claude Haiku               paid API, fast cloud fallback
#   Tier 5  Claude Sonnet              paid API, strongest fallback

Write-Host "`nPulling Qwen2.5-Coder 7B Q4_K_M (GTX 1060 6GB ~4.7GB, fast pre-screener)..." -ForegroundColor Yellow
ollama pull qwen2.5-coder:7b-instruct-q4_K_M

Write-Host "`nPulling Qwen2.5-Coder 14B Q3_K_M (RTX 5060 Ti 8GB ~7.0GB, primary - Q3 keeps full model in VRAM)..." -ForegroundColor Yellow
ollama pull qwen2.5-coder:14b-instruct-q3_K_M

Write-Host "`nPulling Devstral Small 2 24B (both GPUs ~15GB, dense heavy lifter)..." -ForegroundColor Yellow
ollama pull devstral-small-2:24b

Write-Host "`nPulling Qwen3 30B A3B Q4_K_M (both GPUs ~19GB, MoE reasoning alt - ~5GB CPU spill)..." -ForegroundColor Yellow
ollama pull qwen3:30b-a3b-q4_K_M

# ── Python packages ───────────────────────────────────────────────────────────
Write-Host "`nInstalling Python packages..." -ForegroundColor Yellow
pip install anthropic httpx --quiet

# ── PyGhidra (bundled with Ghidra 11.1+) ──────────────────────────────────────
# Installs pyghidra from the portable Ghidra distribution so ghidra_server.py
# can start the headless JVM.  Safe to re-run (pip is idempotent).
$ghidraDir = "E:\Users\PC\dev\ghidra_12.0.4_PUBLIC"
$pyghidraPkg = "$ghidraDir\Ghidra\Features\PyGhidra\pypkg"
if (Test-Path $pyghidraPkg) {
    Write-Host "`nInstalling pyghidra from bundled pypkg..." -ForegroundColor Yellow
    pip install "$pyghidraPkg" --quiet
    Write-Host "pyghidra installed." -ForegroundColor Green
} else {
    Write-Host "`nWARNING: Ghidra not found at $ghidraDir" -ForegroundColor Yellow
    Write-Host "         Download Ghidra 12.0.4 from https://ghidra-sre.org and extract to that path." -ForegroundColor Yellow
    Write-Host "         Then re-run setup_win.ps1 to install pyghidra." -ForegroundColor Yellow
}

# ── GhidraGameCubeLoader extension (Wii/GC PowerPC:BE:32:Gekko_Broadway) ──────
# Extracts directly into the Ghidra install tree so pyghidra headless always
# finds it regardless of user-profile extension scanning quirks.
# Zip top-level folder is "GameCubeLoader" -- that becomes the extension name.
$ghidraExtDir = "$ghidraDir\Ghidra\Extensions"
$extMarker    = "$ghidraExtDir\GameCubeLoader"

if (Test-Path $extMarker) {
    Write-Host "`nGhidraGameCubeLoader already installed." -ForegroundColor Green
} elseif (-not (Test-Path $ghidraDir)) {
    Write-Host "`nSkipping GhidraGameCubeLoader - Ghidra not found at $ghidraDir" -ForegroundColor DarkGray
} else {
    Write-Host "`nDownloading GhidraGameCubeLoader extension..." -ForegroundColor Yellow
    try {
        $headers = @{ "User-Agent" = "setup_win.ps1" }
        if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
        $release = Invoke-RestMethod `
            -Uri "https://api.github.com/repos/Cuyler36/Ghidra-GameCube-Loader/releases/latest" `
            -Headers $headers
        $asset = $release.assets | Where-Object { $_.name -like "*.zip" } | Select-Object -First 1

        if (-not $asset) {
            Write-Host "  No zip asset found - check https://github.com/Cuyler36/Ghidra-GameCube-Loader/releases" -ForegroundColor Yellow
        } else {
            $zipPath = "$env:TEMP\GhidraGameCubeLoader.zip"
            Write-Host "  Fetching $($asset.name) ..." -ForegroundColor DarkGray
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -Headers $headers

            New-Item -ItemType Directory -Force -Path $ghidraExtDir | Out-Null
            Expand-Archive -Path $zipPath -DestinationPath $ghidraExtDir -Force
            Remove-Item $zipPath

            Write-Host "  Installed to $extMarker" -ForegroundColor Green
            Write-Host "  Ghidra headless will now recognise PowerPC:BE:32:Gekko_Broadway." -ForegroundColor Green
        }
    } catch {
        Write-Host "  Failed: $_" -ForegroundColor Yellow
        Write-Host "  Install manually: https://github.com/Cuyler36/Ghidra-GameCube-Loader/releases" -ForegroundColor DarkGray
    }
}

# ── IDA Pro DOL loader ────────────────────────────────────────────────────────
# Installs the Python DOL loader so IDA correctly parses Nintendo .dol files
# instead of misidentifying them as COFF and producing an empty database.
# Also wipes any stale database that was built before the loader was present.
$idaDir     = "C:\Program Files\IDA Professional 9.0"
$idaLoaders = "$idaDir\loaders"
$dolLoader  = "tools\dol_loader.py"
$idaDb0     = "ida_projects\main.dol.id0"

if (-not (Test-Path $idaDir)) {
    Write-Host "`nIDA Pro not found at $idaDir -- skipping DOL loader install" -ForegroundColor DarkGray
    Write-Host "  Set IDA_DIR env var if installed elsewhere." -ForegroundColor DarkGray
} elseif (-not (Test-Path $dolLoader)) {
    Write-Host "`nWARNING: $dolLoader not found -- cannot install DOL loader" -ForegroundColor Yellow
} else {
    $dst = "$idaLoaders\dol_loader.py"
    # Always reinstall — the loader may have been updated
    Write-Host "`nInstalling IDA DOL loader to $idaLoaders ..." -ForegroundColor Yellow
    try {
        Copy-Item $dolLoader $dst -Force
        Write-Host "  Installed: $dst" -ForegroundColor Green

        # Wipe any stale database built without the loader (COFF misparse)
        if (Test-Path $idaDb0) {
            Write-Host "  Removing stale IDA database (rebuilt on next autopilot run) ..." -ForegroundColor DarkGray
            foreach ($ext in @(".id0", ".id1", ".nam", ".til", ".idb")) {
                $f = "ida_projects\main.dol$ext"
                if (Test-Path $f) { Remove-Item $f -Force }
            }
            Write-Host "  Stale database removed." -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  Failed (may need admin): $_" -ForegroundColor Yellow
        Write-Host "  Run this script from an elevated PowerShell, or copy manually:" -ForegroundColor DarkGray
        Write-Host "    Copy-Item $dolLoader $dst" -ForegroundColor DarkGray
    }
}


# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host @"

=== Setup complete ===

Hardware: Ryzen 7700X | RTX 5060 Ti 8GB GDDR7 (GPU 0) + GTX 1060 6GB (GPU 1) | 64GB DDR5

Model ladder:
  0: Qwen2.5-Coder 7B  Q4_K_M  ~4.7GB  GTX 1060   port 11435  (fast pre-screener)
  1: Qwen2.5-Coder 14B Q3_K_M  ~7.0GB  5060 Ti    port 11434  (primary - full VRAM)
  2: Devstral Small 2  24B     ~15GB   both GPUs  port 11434  (dense heavy lifter)
  3: Qwen3 30B A3B Q4_K_M      ~19GB   both GPUs  port 11434  (MoE alt, ~5GB CPU spill)
  4: Claude Haiku                        paid API, fast cloud fallback
  5: Claude Sonnet                       paid API, strongest fallback

To run the autopilot:
  cd E:\Users\PC\dev\accf-decompp
  .\tools\autopilot_win.ps1

mwcceppc.exe runs natively on Windows -- no Wine needed.
"@ -ForegroundColor Cyan
