<#
Vector Lake watchdog wrapper for the scheduled task "VectorLake-Watchdog".

Why this exists: ``watchdog_sync.py`` is a foreground process whose only log sink is stderr,
and Task Scheduler cannot redirect it. This wrapper owns exactly one start -- it pins the
working directory to the repository root, resolves the interpreter, pins UTF-8 so Chinese
page names survive in the log, and then runs the watchdog in the foreground until it exits.

The task's repetition trigger runs with MultipleInstances=IgnoreNew, so it only starts a new
wrapper after the previous one returned: one running watchdog, and a bounded relaunch after a
crash or a hard kill. The watchdog itself keeps exactly one instance through
``.meta/.watchdog.instance.lock``, so a manual double start is rejected, not duplicated.
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [int]$KeepLogs = 10,
    [string]$MemoryDir = '',
    [string]$PayloadRoot = '',
    [ValidateSet('0', '1')]
    [string]$RunnerAutostart,
    [ValidateRange(0, 2147483647)]
    [Nullable[int]]$CatchupIntervalSeconds
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$scratch = Join-Path $root 'scratch'
New-Item -ItemType Directory -Path $scratch -Force | Out-Null

if ($Python) {
    $exe = $Python
}
else {
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    $exe = if (Test-Path $candidate) { $candidate } else { (Get-Command python.exe).Source }
}
if (-not (Test-Path $exe)) { throw "python interpreter not found: $exe" }

# A self-healing task can restart many times in a day; keep the newest N per stream.
foreach ($stream in 'out', 'err') {
    Get-ChildItem -Path $scratch -Filter "watchdog_service-*-$stream.log" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $KeepLogs |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path $scratch "watchdog_service-$stamp-out.log"
$err = Join-Path $scratch "watchdog_service-$stamp-err.log"

# The MCP server runs with the same pair; without them the log mangles non-ASCII page names.
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = $root
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONOPTIMIZE = '0'

# Task-scoped settings, not global environment changes. Unbound settings retain
# the inherited environment/runtime defaults.
foreach ($binding in @{
    VECTOR_LAKE_MEMORY_DIR = $MemoryDir
    VECTOR_LAKE_PAYLOAD_ROOT = $PayloadRoot
}.GetEnumerator()) {
    if ($binding.Value) {
        $directory = Get-Item -LiteralPath $binding.Value -ErrorAction Stop
        if (-not $directory.PSIsContainer) { throw "$($binding.Key) must name a directory" }
        [Environment]::SetEnvironmentVariable($binding.Key, $directory.FullName, 'Process')
    }
}
if ($PSBoundParameters.ContainsKey('RunnerAutostart')) {
    $env:VECTOR_LAKE_RUNNER_AUTOSTART = $RunnerAutostart
}
if ($PSBoundParameters.ContainsKey('CatchupIntervalSeconds')) {
    $env:VECTOR_LAKE_CATCHUP_INTERVAL_SECONDS = [string]$CatchupIntervalSeconds
}

# Interactive latency trigger for the memory n-gram index.
#
# The write-count threshold (500 documents) is a *write-lock* budget, not a latency budget: it
# decides how often a rebuild may stop writers, and it ignores that a stale index turns every
# memory-backed query into an O(rows x terms) scan.  Measured 2026-09-29 on the live lake: 458
# dirty documents sat below that threshold, the read path had switched the indexed path off, and
# nothing was scheduled to fix it -- every affected query paid ~0.5 s (rebuild afterwards took
# ``assemble_context`` from 669.9 ms to 63.2 ms).
#
# 20 searches is deliberately far below the ~166-search machine-time break-even: this host serves a
# human, so user-visible latency outranks aggregate CPU time, while 20 still bounds rebuild churn to
# roughly one rebuild per 20 searches.  Export the variable yourself (including ``0``) to override;
# the guard below only supplies the default.
if (-not $env:VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES) {
    $env:VECTOR_LAKE_MEMORY_GRAM_LATENCY_SEARCHES = '20'
}

Set-Location $root

$process = Start-Process -FilePath $exe -ArgumentList 'watchdog_sync.py' `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -PassThru -Wait

exit $process.ExitCode
