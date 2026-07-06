# clean.ps1 - Cleanup helper invoked by ..\clean.bat.
# Removes local pytest/pycache dirs, upstream debug dumps, ad-hoc debug scripts,
# log files, and optionally the proxy virtualenv.

param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$RemoveVenv,
    [switch]$StopProxy
)

$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path -LiteralPath $Root).Path
$ProxyDir = Join-Path $Root 'claude-proxy'

if (-not (Test-Path -LiteralPath $ProxyDir)) {
    Write-Host ("[ERROR] Expected folder not found: `"{0}`"" -f $ProxyDir)
    exit 1
}

Write-Host '[INFO] Cleaning Flowith Claude/Codex Proxy local artifacts...'
Write-Host ("[INFO] Project: `"{0}`"" -f $Root)
Write-Host ''

function Remove-EmptyLock {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host '[OK] Dependency install lock not found.'
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        $entries = Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($entries) {
            Write-Host ("[WARN] Dependency install lock is not empty; leaving it in place: `"{0}`"" -f $Path)
            return
        }
        try {
            Remove-Item -LiteralPath $Path -Force -Recurse -ErrorAction Stop
            Write-Host '[OK] Removed dependency install lock.'
        }
        catch {
            Write-Host ("[WARN] Could not remove dependency install lock: {0}" -f $_.Exception.Message)
        }
    }
    else {
        Write-Host ("[WARN] Install lock path is a file, not a directory; leaving it: `"{0}`"" -f $Path)
    }
}

function Remove-Tree {
    param(
        [string]$Path,
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host ("[OK] {0} not found." -f $Label)
        return
    }
    try {
        Write-Host ("[INFO] Removing {0}..." -f $Label)
        Remove-Item -LiteralPath $Path -Force -Recurse -ErrorAction Stop
        if (Test-Path -LiteralPath $Path) {
            Write-Host ("[WARN] Could not remove {0}: `"{1}`"" -f $Label, $Path)
        }
        else {
            Write-Host ("[OK] Removed {0}." -f $Label)
        }
    }
    catch {
        Write-Host ("[WARN] Could not remove {0}: {1}" -f $Label, $_.Exception.Message)
    }
}

function Stop-ProxyListeners {
    $ports = @(8787, 8788, 8789)
    $stopped = 0
    foreach ($port in $ports) {
        $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -eq 0) {
            continue
        }
        foreach ($listener in $listeners) {
            $pidValue = [int]$listener.OwningProcess
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
            $command = if ($proc) { [string]$proc.CommandLine } else { '' }
            if ($command -notmatch 'python(\.exe)?"?\s+-m\s+proxy') {
                Write-Host ("[WARN] Port {0} is held by PID {1}, but it does not look like this proxy; leaving it running." -f $port, $pidValue)
                continue
            }
            try {
                Stop-Process -Id $pidValue -ErrorAction Stop
                $stopped++
                Write-Host ("[OK] Stopped proxy on port {0} (PID {1})." -f $port, $pidValue)
            }
            catch {
                Write-Host ("[WARN] Could not stop proxy PID {0}: {1}" -f $pidValue, $_.Exception.Message)
            }
        }
    }
    if ($stopped -eq 0) {
        Write-Host '[OK] No running proxy processes stopped.'
    }
    elseif ($stopped -gt 0) {
        Start-Sleep -Milliseconds 500
    }
}

function Test-FileInUse {
    param([string]$Path)
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        return $false
    }
    catch [System.IO.IOException] {
        return $true
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Remove-Glob {
    param(
        [string]$Directory,
        [string]$Pattern,
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Directory)) {
        Write-Host ("[OK] {0} skipped (dir missing)." -f $Label)
        return
    }
    $items = @(Get-ChildItem -LiteralPath $Directory -Filter $Pattern -File -ErrorAction SilentlyContinue)
    if ($items.Count -eq 0) {
        Write-Host ("[OK] {0} not found." -f $Label)
        return
    }
    $removed = 0
    foreach ($f in $items) {
        if (Test-FileInUse -Path $f.FullName) {
            Write-Host ("[INFO] Keeping in-use file: {0}" -f $f.FullName)
            continue
        }
        try {
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $f.FullName)) {
                $removed++
                Write-Host ("[OK] del {0}" -f $f.Name)
            }
            else {
                Write-Host ("[WARN] Could not remove: {0}" -f $f.FullName)
            }
        }
        catch {
            Write-Host ("[WARN] Could not remove {0}: {1}" -f $f.FullName, $_.Exception.Message)
        }
    }
    if ($removed -gt 0) {
        Write-Host ("[OK] Removed {0} item(s) - {1}." -f $removed, $Label)
    }
}

if ($StopProxy) {
    Write-Host '[INFO] Stopping local proxy processes before cleanup...'
    Stop-ProxyListeners
}

Remove-EmptyLock (Join-Path $ProxyDir '.install.lock')
Remove-Tree (Join-Path $Root       '.pytest_cache')          'root pytest cache'
Remove-Tree (Join-Path $ProxyDir   '.pytest_cache')          'proxy pytest cache'
Remove-Tree (Join-Path $ProxyDir   'proxy\__pycache__')      'proxy bytecode cache'
Remove-Tree (Join-Path $ProxyDir   'tests\__pycache__')      'test bytecode cache'
Remove-Tree (Join-Path $ProxyDir   'debug_dumps')            'upstream debug dumps'

Remove-Glob $Root     '*.log'         'root log files'
Remove-Glob $ProxyDir '*.log'         'proxy log files'
Remove-Glob $Root     '_apply_*.py'   'root apply helper scripts'
Remove-Glob $Root     '_categorize.py' 'root categorize helper scripts'
Remove-Glob $Root     '_check_*.py'   'root check helper scripts'
Remove-Glob $Root     '_fix_*.py'     'root fix helper scripts'
Remove-Glob $Root     '_inspect_*.py' 'root inspect helper scripts'
Remove-Glob $Root     '_regress.py'   'root regression helper scripts'
Remove-Glob $Root     '_replay.py'    'root replay helper scripts'
Remove-Glob $Root     '_scratch_*.py' 'root scratch scripts'
Remove-Glob $Root     '_repro_*.py'   'root repro scripts'
Remove-Glob $Root     '_patch_*.py'   'root ad-hoc patch scripts'
Remove-Glob $ProxyDir '_apply_*.py'   'proxy apply helper scripts'
Remove-Glob $ProxyDir '_check_*.py'   'proxy check helper scripts'
Remove-Glob $ProxyDir '_fix_*.py'     'proxy fix helper scripts'
Remove-Glob $ProxyDir '_inspect_*.py' 'proxy inspect helper scripts'
Remove-Glob $ProxyDir '_scratch_*.py' 'proxy scratch scripts'
Remove-Glob $ProxyDir '_repro_*.py'   'proxy repro scripts'
Remove-Glob $ProxyDir '_patch_*.py'   'proxy ad-hoc patch scripts'
Remove-Glob (Join-Path $ProxyDir 'proxy') '*.bak' 'proxy backup files'

if ($RemoveVenv) {
    Remove-Tree (Join-Path $ProxyDir 'venv') 'virtual environment'
}
else {
    Write-Host '[INFO] Keeping venv. Run clean.bat --venv to force dependency reinstall on next launch.'
}

Write-Host ''
Write-Host '[DONE] Clean complete.'
exit 0
