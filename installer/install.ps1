# Copper for KiCad - one-line installer for Windows (PowerShell 5.1+).
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/claynicholson/copper-kicad-mcp/main/installer/install.ps1 | iex"
#
# Installs uv (if missing), then Copper into its own Python 3.12 environment
# (`uv tool install`, per-user, no admin) from the wheel attached to the GitHub
# release (checked against the release's SHA256SUMS.txt), then runs `copper setup`,
# which asks before each change: library tables, KiCad API (only if KiCad is closed),
# AI-client registration, Freerouting, doctor. With redirected input (CI) it uses --yes.
#
# Options (environment variables):
#   COPPER_VERSION=1.1.1      install a specific release (default: the latest GitHub release)
#   COPPER_SOURCE=<spec>      anything `uv tool install` accepts (a wheel path or URL); skips the download
#   COPPER_SETUP_ARGS="..."   arguments for `copper setup` (default: none = interactive; --yes when input is redirected)
#   COPPER_NO_SETUP=1         install only
#   COPPER_NO_MODIFY_PATH=1   do not let uv add its tool folder to your user PATH

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Repo = 'claynicholson/copper-kicad-mcp'
$DefaultVersion = '1.1.1'          # used when the GitHub API cannot be reached (rate limit, proxy)
function Say($msg) { Write-Host "  " -NoNewline; Write-Host "::" -ForegroundColor DarkYellow -NoNewline; Write-Host " $msg" }
function Fail($msg) { Write-Host "  x $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Say "Copper for KiCad - installer"
Write-Host ""

# 1. uv
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Say "Installing uv (Astral's Python manager, per-user)..."
    if ($env:COPPER_NO_MODIFY_PATH) { $env:UV_NO_MODIFY_PATH = '1' }   # uv's installer leaves PATH alone too
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if (-not $uv) { Fail "uv could not be installed. See https://docs.astral.sh/uv/getting-started/installation/" }
}
Say "uv: $uv"

# 2. what to install: the wheel attached to the GitHub release
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("copper-install-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $tmp | Out-Null
$spec = $env:COPPER_SOURCE
if (-not $spec) {
    $ver = $env:COPPER_VERSION
    $whlUrl = $null; $sumsUrl = $null
    try {
        $api = if ($ver) { "https://api.github.com/repos/$Repo/releases/tags/v$ver" } else { "https://api.github.com/repos/$Repo/releases/latest" }
        $rel = Invoke-RestMethod -Uri $api -Headers @{ 'User-Agent' = 'copper-installer' }
        $whl = $rel.assets | Where-Object { $_.name -like 'copper_kicad-*.whl' } | Select-Object -First 1
        $sums = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
        if ($whl) { $whlUrl = $whl.browser_download_url }
        if ($sums) { $sumsUrl = $sums.browser_download_url }
    } catch { }
    if (-not $whlUrl) {
        if (-not $ver) { $ver = $DefaultVersion }
        $whlUrl = "https://github.com/$Repo/releases/download/v$ver/copper_kicad-$ver-py3-none-any.whl"
        $sumsUrl = "https://github.com/$Repo/releases/download/v$ver/SHA256SUMS.txt"
    }
    $name = [IO.Path]::GetFileName($whlUrl)
    $spec = Join-Path $tmp $name
    Say "Downloading $name"
    try { Invoke-WebRequest -Uri $whlUrl -OutFile $spec -UseBasicParsing -Headers @{ 'User-Agent' = 'copper-installer' } }
    catch { Fail "could not download $whlUrl ($($_.Exception.Message))" }
    # the wheel must match the checksum published with the same release
    try {
        $sumsFile = Join-Path $tmp 'SHA256SUMS.txt'
        Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsFile -UseBasicParsing -Headers @{ 'User-Agent' = 'copper-installer' }
        $line = Get-Content $sumsFile | Where-Object { $_ -match ("\s\*?" + [Regex]::Escape($name) + "$") } | Select-Object -First 1
    } catch { $line = $null }
    if (-not $line) { Fail "the release's SHA256SUMS.txt does not list $name; not installing an unverified file" }
    $want = ($line -split '\s+')[0].ToLower()
    $got = (Get-FileHash -Algorithm SHA256 $spec).Hash.ToLower()
    if ($want -ne $got) { Fail "checksum mismatch for $name (expected $want, got $got); not installing it" }
    Say "Checksum verified ($name)"
}
Say "Installing Copper from $spec"

# 3. install
# A running Copper server (started by Claude Desktop, Cursor, ...) locks its environment;
# `uv tool install --force` would then leave it half-deleted. Stop those processes first.
try {
    $toolDir = (& $uv tool dir).Trim()
    $roots = @((Join-Path $toolDir 'copper-kicad'), (Join-Path ((& $uv tool dir --bin).Trim()) 'copper.exe'))
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $p = $_.Path; $p -and ($roots | Where-Object { $p.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }) })
    if ($running.Count -gt 0) {
        Say "Stopping $($running.Count) running Copper server process(es) so they can be upgraded (restart your AI apps afterwards)."
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
} catch { }
& $uv tool install --force --python 3.12 $spec
if ($LASTEXITCODE -ne 0) { Fail "uv tool install failed (exit $LASTEXITCODE)." }
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue

$bin = (& $uv tool dir --bin).Trim()
$copper = Join-Path $bin 'copper.exe'
if (-not (Test-Path $copper)) { Fail "copper.exe not found in $bin" }
# Under ErrorActionPreference=Stop, Windows PowerShell 5.1 turns ANY stderr line of a
# redirected native command into a terminating error - and uv reports on stderr.
$ErrorActionPreference = 'Continue'
if (-not $env:COPPER_NO_MODIFY_PATH) { try { & $uv tool update-shell *> $null } catch { } }
$ErrorActionPreference = 'Stop'

# 4. setup
& $copper version | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "copper.exe was installed but does not run (exit $LASTEXITCODE). Re-run this installer." }
if ($env:COPPER_NO_SETUP) { Say "Installed. Run 'copper setup' when you are ready."; exit 0 }
$setupArgs = if ($env:COPPER_SETUP_ARGS) { $env:COPPER_SETUP_ARGS -split ' ' }
             elseif ([Console]::IsInputRedirected) { @('--yes') }   # nobody to ask (CI)
             else { @() }                                            # asks before each change
& $copper setup @setupArgs
$rc = $LASTEXITCODE
Write-Host ""
if ($rc -ne 0) { Say "Installed, but setup reported problems - run 'copper doctor' for the fixes." }
else { Say "Done. Open a new terminal to use 'copper' (copper doctor, copper status)." }
