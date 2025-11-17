Param()

# -------------------------------
# Resolve game directory
# -------------------------------
$GameDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $GameDir

Write-Host "=== Auto-VO WeiDU Installer (PowerShell) ==="
Write-Host "Game dir: $GameDir"
Write-Host ""

# -------------------------------
# Locate WeiDU
# -------------------------------
$weiduExe = Join-Path $GameDir "weidu.exe"
if (-not (Test-Path $weiduExe)) {
    Write-Host "Error: weidu.exe not found in $GameDir" -ForegroundColor Red
    exit 1
}

Write-Host "WeiDU   : $weiduExe"
Write-Host ""

# -------------------------------
# Ensure autovo directory exists
# -------------------------------
$autovoDir = Join-Path $GameDir "autovo"
if (-not (Test-Path $autovoDir)) {
    Write-Host "Error: 'autovo' directory not found in $GameDir" -ForegroundColor Red
    exit 1
}

# Root for combined packs: autovo\packs\<PackName>\...
$packsRoot = Join-Path $autovoDir "packs"

# -------------------------------
# Find individual setup-autovo_*.tp2 (excluding packs)
# -------------------------------
$individualTp2 = Get-ChildItem -Path $autovoDir -Filter "setup-autovo_*.tp2" -Recurse -ErrorAction SilentlyContinue |
    Where-Object {
        if (Test-Path $packsRoot) {
            $_.FullName -notlike ($packsRoot + "*")
        } else {
            $true
        }
    }

# -------------------------------
# Discover pack definitions
# Each pack is a directory under autovo\packs
# containing one or more setup-autovo_*.tp2
# -------------------------------
$packDefs = @()

if (Test-Path $packsRoot) {
    $packDirs = Get-ChildItem -Path $packsRoot -Directory -ErrorAction SilentlyContinue
    foreach ($p in $packDirs) {
        $packTp2 = Get-ChildItem -Path $p.FullName -Filter "setup-autovo_*.tp2" -Recurse -ErrorAction SilentlyContinue
        if ($packTp2 -and $packTp2.Count -gt 0) {
            $packDefs += [PSCustomObject]@{
                PackName = $p.Name
                Files    = $packTp2
            }
        }
    }
}

# Nothing at all?
if ((-not $individualTp2 -or $individualTp2.Count -eq 0) -and ($packDefs.Count -eq 0)) {
    Write-Host "No Auto-VO components or packs found under $autovoDir" -ForegroundColor Yellow
    exit 0
}

# Normalize to arrays
if ($individualTp2 -and $individualTp2 -isnot [System.Array]) {
    $individualTp2 = @($individualTp2)
}

# -------------------------------
# Build unified menu
#   - Individual components
#   - Combined packs
# -------------------------------
$menu = @()
$index = 0

if ($individualTp2 -and $individualTp2.Count -gt 0) {
    Write-Host "Individual Auto-VO components:`n"
    foreach ($tp2 in $individualTp2) {
        $index++
        # setup-autovo_dscpat4.tp2 -> autovo_dscpat4
        $base = $tp2.Name
        $compName = $base -replace '^setup-', '' -replace '\.tp2$', ''

        $menu += [PSCustomObject]@{
            Index = $index
            Kind  = "Individual"
            Label = $compName
            Files = @($tp2.FullName)
        }

        Write-Host ("  [{0}] {1}" -f $index, $compName)
    }
}

if ($packDefs.Count -gt 0) {
    Write-Host ""
    Write-Host "Combined Auto-VO packs:`n"
    foreach ($pack in $packDefs) {
        $index++
        $packLabel = "Pack: {0}" -f $pack.PackName

        $menu += [PSCustomObject]@{
            Index = $index
            Kind  = "Pack"
            Label = $packLabel
            Files = $pack.Files.FullName
        }

        Write-Host ("  [{0}] {1} (includes {2} component(s))" -f $index, $packLabel, $pack.Files.Count)
    }
}

Write-Host ""
Write-Host "  [0] Install ALL individual components"
Write-Host "  [Q] Quit"
Write-Host ""

# -------------------------------
# Read selection
# -------------------------------
$choice = Read-Host "Select item to install (0-$index or Q)"

if ($choice -match '^[Qq]$') {
    Write-Host "Aborting."
    exit 0
}

if ($choice -eq '0') {
    if (-not $individualTp2 -or $individualTp2.Count -eq 0) {
        Write-Host "No individual components to install." -ForegroundColor Yellow
        exit 0
    }

    Write-Host ""
    Write-Host "Installing ALL individual Auto-VO components..." -ForegroundColor Cyan

    foreach ($entry in $menu | Where-Object { $_.Kind -eq "Individual" }) {
        Write-Host ("  -> {0}" -f $entry.Label)
        & $weiduExe $entry.Files[0]
    }

    Write-Host "`nDone."
    exit 0
}

if (-not ($choice -match '^\d+$')) {
    Write-Host "Invalid selection." -ForegroundColor Red
    exit 1
}

[int]$choiceIndex = [int]$choice
if ($choiceIndex -lt 1 -or $choiceIndex -gt $menu.Count) {
    Write-Host "Selection out of range." -ForegroundColor Red
    exit 1
}

$selected = $menu | Where-Object { $_.Index -eq $choiceIndex }

Write-Host ""

if ($selected.Kind -eq "Individual") {
    Write-Host ("Installing {0} ..." -f $selected.Label) -ForegroundColor Cyan
    & $weiduExe $selected.Files[0]
    Write-Host "`nDone."
    exit 0
}

if ($selected.Kind -eq "Pack") {
    Write-Host ("Installing {0} ..." -f $selected.Label) -ForegroundColor Cyan
    foreach ($f in $selected.Files) {
        $name = Split-Path $f -Leaf
        Write-Host ("  -> {0}" -f $name)
        & $weiduExe $f
    }
    Write-Host "`nDone."
    exit 0
}

Write-Host "Internal error: unknown menu entry type." -ForegroundColor Red
exit 1
