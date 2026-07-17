# clean.ps1 - Cleanup helper invoked by ..\clean.bat.
# Removes local pytest/pycache dirs, upstream debug dumps, ad-hoc debug scripts,
# log files, and optionally the proxy virtualenv. Can also stop local proxy listeners.

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

function Test-IsProxyProcess {
    param([string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    return ($CommandLine -match 'python(\.exe)?"?\s+-m\s+proxy') -or ($CommandLine -match 'pythonw(\.exe)?"?\s+-m\s+proxy')
}

function Stop-ProxyListeners {
    $ports = @(8787, 8788, 8789)
    $stopped = @{}
    foreach ($port in $ports) {
        $listeners = @()
        try {
            $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        }
        catch {
            $listeners = @()
        }
        if ($listeners.Count -eq 0) {
            # Fallback for environments where Get-NetTCPConnection is restricted.
            $net = netstat -ano | Select-String (":$port\s+.*LISTENING")
            foreach ($line in $net) {
                $parts = @(($line.ToString() -split '\s+') | Where-Object { $_ })
                if ($parts.Count -ge 5 -and $parts[-1] -match '^\d+$') {
                    $listeners += [pscustomobject]@{ OwningProcess = [int]$parts[-1] }
                }
            }
        }
        foreach ($listener in $listeners) {
            $pidValue = [int]$listener.OwningProcess
            if ($stopped.ContainsKey($pidValue)) {
                continue
            }
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
            $command = if ($proc) { [string]$proc.CommandLine } else { '' }
            if (-not (Test-IsProxyProcess -CommandLine $command)) {
                Write-Host ("[WARN] Port {0} is held by PID {1}, but it does not look like this proxy; leaving it running." -f $port, $pidValue)
                continue
            }
            try {
                Stop-Process -Id $pidValue -Force -ErrorAction Stop
                $stopped[$pidValue] = $true
                Write-Host ("[OK] Stopped proxy on port {0} (PID {1})." -f $port, $pidValue)
            }
            catch {
                $kill = Start-Process -FilePath 'taskkill.exe' -ArgumentList @('/PID', "$pidValue", '/F', '/T') -Wait -PassThru -WindowStyle Hidden
                if ($kill.ExitCode -eq 0) {
                    $stopped[$pidValue] = $true
                    Write-Host ("[OK] Force-stopped proxy on port {0} (PID {1})." -f $port, $pidValue)
                }
                else {
                    Write-Host ("[WARN] Could not stop proxy PID {0}: {1}" -f $pidValue, $_.Exception.Message)
                }
            }
        }
    }

    # Catch detached python -m proxy processes that no longer own a listen socket.
    $orphans = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^python' -and (Test-IsProxyProcess -CommandLine ([string]$_.CommandLine))
    }
    foreach ($proc in $orphans) {
        $pidValue = [int]$proc.ProcessId
        if ($stopped.ContainsKey($pidValue)) {
            continue
        }
        try {
            Stop-Process -Id $pidValue -Force -ErrorAction Stop
            $stopped[$pidValue] = $true
            Write-Host ("[OK] Stopped orphan proxy process PID {0}." -f $pidValue)
        }
        catch {
            Start-Process -FilePath 'taskkill.exe' -ArgumentList @('/PID', "$pidValue", '/F', '/T') -Wait -WindowStyle Hidden | Out-Null
            Write-Host ("[OK] Force-stopped orphan proxy process PID {0}." -f $pidValue)
            $stopped[$pidValue] = $true
        }
    }

    if ($stopped.Count -eq 0) {
        Write-Host '[OK] No running proxy processes stopped.'
    }
    else {
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
Remove-Tree (Join-Path $ProxyDir   'proxy\codex\__pycache__') 'codex bytecode cache'
Remove-Tree (Join-Path $ProxyDir   'tests\__pycache__')      'test bytecode cache'
Remove-Tree (Join-Path $ProxyDir   'debug_dumps')            'upstream debug dumps'

Remove-Glob $Root     '*.log'         'root log files'
Remove-Glob $ProxyDir '*.log'         'proxy log files'
# No tracked top-level file starts with '_', so an underscore prefix marks
# an ad-hoc debug artifact (scripts, request/response dumps, probe output).
Remove-Glob $Root     '_*.py'          'root ad-hoc debug scripts'
Remove-Glob $Root     '_*.json'        'root debug JSON dumps'
Remove-Glob $Root     '_*.txt'         'root debug text dumps'
Remove-Glob $Root     '_*.out'         'root debug output files'
Remove-Glob $ProxyDir '_*.py'          'proxy ad-hoc debug scripts'
Remove-Glob $ProxyDir '_*.json'        'proxy debug JSON dumps'
Remove-Glob $ProxyDir '_*.txt'         'proxy debug text dumps'
Remove-Glob $ProxyDir '_*.out'         'proxy debug output files'
Remove-Glob $ProxyDir '_*.bat'         'proxy ad-hoc launcher scripts'
Remove-Glob $ProxyDir '_*.ps1'         'proxy ad-hoc powershell scripts'
Remove-Glob $ProxyDir '*_dump.txt'     'proxy stream dumps'
Remove-Glob (Join-Path $ProxyDir 'proxy') '*.bak' 'proxy backup files'

if ($RemoveVenv) {
    Remove-Tree (Join-Path $ProxyDir 'venv') 'virtual environment'
}
else {
    Write-Host '[INFO] Keeping venv. Run clean.bat --venv to force dependency reinstall on next launch.'
}

Write-Host ''
Write-Host '[DONE] Clean complete.'
Write-Host '[TIP] Restart Codex proxy with start-codex.bat (port 8788).'
exit 0
