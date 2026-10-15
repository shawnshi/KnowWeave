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
    [Nullable[int]]$CatchupIntervalSeconds,
    # Foreground program to own; the default is the watchdog. Exposed so the containment
    # contract below can be exercised with a sentinel instead of the daemon itself.
    [string]$EntryScript = 'watchdog_sync.py'
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

# Assign this wrapper before starting Python: every child inherits the Job at creation,
# including Windows venv redirectors. The private handle is held only by this wrapper;
# KILL_ON_JOB_CLOSE releases all descendants when the action exits or is terminated.
# Fail closed BEFORE launching the payload if the kernel cannot establish containment.
if (-not ('VL.Job' -as [type])) {
    Add-Type -Namespace VL -Name Job -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)] public struct IO_COUNTERS {
    public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
[StructLayout(LayoutKind.Sequential)] public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
    public long PerProcessUserTimeLimit;
    public long PerJobUserTimeLimit;
    public uint LimitFlags;
    public UIntPtr MinimumWorkingSetSize;
    public UIntPtr MaximumWorkingSetSize;
    public uint ActiveProcessLimit;
    public UIntPtr Affinity;
    public uint PriorityClass;
    public uint SchedulingClass; }
[StructLayout(LayoutKind.Sequential)] public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation; public IO_COUNTERS IoInfo;
    public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }
[DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr CreateJobObject(IntPtr attributes, string name);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool QueryInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length, out uint returned);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
'@
}

$job = [VL.Job]::CreateJobObject([IntPtr]::Zero, $null)
if ($job -eq [IntPtr]::Zero) { throw 'CreateJobObject failed' }
$info = New-Object 'VL.Job+JOBOBJECT_EXTENDED_LIMIT_INFORMATION'
# BasicLimitInformation is a value type. Mutating a nested field alone changes a copy
# in PowerShell; explicitly write the entire value back into the outer structure.
$limits = $info.BasicLimitInformation
$limits.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
$info.BasicLimitInformation = $limits
$size = [Runtime.InteropServices.Marshal]::SizeOf($info)
$buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal($size)
try {
    [Runtime.InteropServices.Marshal]::StructureToPtr($info, $buffer, $false)
    if (-not [VL.Job]::SetInformationJobObject($job, 9, $buffer, [uint32]$size)) {
        throw ('SetInformationJobObject failed: Win32={0}' -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    [uint32]$returned = 0
    if (-not [VL.Job]::QueryInformationJobObject($job, 9, $buffer, [uint32]$size, [ref]$returned)) {
        throw ('QueryInformationJobObject failed: Win32={0}' -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    # LimitFlags follows two LARGE_INTEGER fields (offset 16). Avoid PowerShell's
    # ambiguous PtrToStructure(IntPtr, object)/(IntPtr, Type) overload binding.
    $actualFlags = [Runtime.InteropServices.Marshal]::ReadInt32($buffer, 16)
    if (($actualFlags -band 0x2000) -eq 0) {
        throw 'Kernel Job does not have KILL_ON_JOB_CLOSE; refusing to launch'
    }
}
finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer) }
if (-not [VL.Job]::AssignProcessToJobObject($job, [Diagnostics.Process]::GetCurrentProcess().Handle)) {
    throw ('AssignProcessToJobObject(wrapper) failed before launch: Win32={0}' -f [Runtime.InteropServices.Marshal]::GetLastWin32Error())
}

$process = Start-Process -FilePath $exe -ArgumentList ('"{0}"' -f $EntryScript) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -PassThru

# Pin the handle before waiting: Windows PowerShell 5.1 Start-Process with stream
# redirection otherwise releases it and reports a null ExitCode (which `exit` maps to 0).
$null = $process.Handle
# Do not close the Job explicitly: this wrapper itself is a member. Process exit closes
# its private handle after the requested exit code has been set, killing leftover children.
$process.WaitForExit()
$exitCode = $process.ExitCode
if ($null -eq $exitCode) { throw 'Child exit code unavailable' }
exit $exitCode
